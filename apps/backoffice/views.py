"""Views for the internal backoffice.

This is the internal UI for all three case types - storage,
transport and service each keep their models and services in their own
app and none of them ship a view, a form or a template. The admin is a
fallback for configuration rows, not the working surface.
"""

import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import ValidationError
from django.db.models import Count, Prefetch, Q
from django.http import QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import formats
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import DetailView, ListView, TemplateView

from apps.accounts.models import ApprovalLimit, ApprovalLimitChange, Role
from apps.accounts.services import (
    LADDER_EDIT_ROLES,
    LADDER_ROLES,
    may_edit_approval_ladder,
    record_approval_limit_change,
)
from apps.attachments.services import (
    attach_file,
    attachments_for,
    group_attachments_by_kind,
)
from apps.cases.exceptions import CaseWorkflowError
from apps.cases.models import Case, CaseType, ContactRole, Status
from apps.cases.services import (
    SETTLEMENT_ROLES,
    add_activity_entry,
    auto_advance_case,
    available_transitions,
    closed_case_message,
    case_attachments,
    create_staff_case,
    has_reached_status,
    record_client_assessment,
    record_contact_person,
    record_report_location,
    record_delivery_method,
    record_work_location_consult,
    set_financial_settlement,
    transition_case,
    asset_attachments,
)
from apps.clients.models import Client, Asset
from apps.clients.services import (
    record_asset_identification,
    suggest_matches,
)
from apps.intake.models import HandoverRecord, IntakeItem, IntakeRecord
from apps.service.models import InspectionRecord
from apps.intake.services import (
    add_intake_item,
    append_intake_amendment,
    attach_handover_approval,
    compare_handover_with_intake,
    confirm_no_intake_items,
    create_handover_record,
    create_intake_record,
    has_handover_approval,
    has_handover_photos,
    remove_intake_item,
    sign_handover,
    sign_handover_technician,
    sign_intake_record,
    update_handover_note,
)
from apps.parts.models import PartOrder
from apps.parts.services import (
    create_catalogue_part,
    order_part_with_advance,
    receive_part_order,
)
from apps.quotes.models import AdvancePayment, Approval, Quote
from apps.quotes.services import (
    accept_fully_approved_quotes,
    create_quote,
    log_price_list_basis,
    mark_advance_paid,
    missing_approval_levels,
    record_approval,
    request_advance_payment,
    send_quote,
)
from apps.scheduling.services import book_appointment
from apps.service.services import (
    CostUnknown,
    attach_client_approval,
    create_work_order,
    finalize_work_order,
    has_client_approval,
    log_work,
    record_diagnostic,
    record_test_run,
    record_used_part,
    reopen_work_order,
    send_completion_notice,
    sign_work_order,
    work_cost_so_far,
    is_new_client,
    record_inspection_results,
    sign_inspection_record,
    start_inspection,
)
from apps.transport.services import (
    attach_delivery_protocol,
    declare_carrier_price,
    ensure_transport_advance,
    has_delivery_protocol,
    mark_delivered,
    missing_plan_items,
    recommended_mode,
    record_delivery_confirmation,
    record_loading_confirmation,
    record_loading_plan,
)
from apps.storage.models import (
    StorageAccommodation,
    StorageAgreement,
    StorageOrientation,
    StoragePriceBand,
    StorageZone,
)
from apps.storage.services import (
    assign_position,
    create_storage_offer,
    outline_path,
    place_asset,
    unplace_asset,
    price_for,
    record_site_pickup,
    upsert_site_pickup,
    record_price_deviation,
    record_storage_entry,
    suggest_offer,
    asset_footprint_size,
    zone_footprint,
)

from .forms import (
    ActivityLogForm,
    AppointmentBookForm,
    ApprovalLimitForm,
    AttachmentUploadForm,
    CaseCreateForm,
    ClientAssessmentForm,
    CompletionNoticeForm,
    PartQuickAddForm,
    WorkOrderReopenForm,
    DeliveryConfirmationForm,
    DeliveryProtocolUploadForm,
    DiagnosticCreateForm,
    HandoverApprovalUploadForm,
    HandoverMediaUploadForm,
    HandoverSignForm,
    InspectionResultsForm,
    IntakeAmendmentForm,
    IntakeItemForm,
    IntakeRecordCreateForm,
    LoadingConfirmationForm,
    LoadingPlanForm,
    SitePickupForm,
    PartOrderCreateForm,
    PriceBandForm,
    QuoteAdvanceForm,
    QuoteCreateForm,
    ContactPersonForm,
    ReportLocationForm,
    ServicePriceItemForm,
    AssetIdentityForm,
    StorageAgreementForm,
    StorageOfferForm,
    StoragePlacementForm,
    StoragePositionAssignForm,
    TestRunForm,
    TransportCarrierPriceForm,
    UsedPartForm,
    WorkDocsUploadForm,
    DeliveryMethodForm,
    WorkLocationForm,
    WorkLogEntryForm,
    WorkOrderApprovalUploadForm,
)


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Backoffice views are for staff accounts only: an authenticated
    non-staff user gets 403 instead of the internal UI."""

    def test_func(self):
        return self.request.user.is_staff

    def handle_no_permission(self):
        """An expired session during an htmx save used to swap the login
        form into ``#main`` under a header still showing the user, with
        ``next=`` pointing at the POST-only action URL - so signing in
        landed on "405" (QA UI-M5). For htmx requests, send the browser to
        the login page in full, coming back to the page it was on."""
        request = self.request
        if (
            not request.user.is_authenticated
            and request.headers.get("HX-Request") == "true"
        ):
            from urllib.parse import urlsplit

            from django.contrib.auth.views import redirect_to_login
            from django.http import HttpResponse

            current = urlsplit(request.headers.get("HX-Current-URL", ""))
            back = current.path or reverse("backoffice:home")
            if current.query:
                back += "?" + current.query
            login = redirect_to_login(back, self.get_login_url())
            response = HttpResponse(status=204)
            response["HX-Redirect"] = login["Location"]
            return response
        return super().handle_no_permission()


class HomeView(StaffRequiredMixin, TemplateView):
    """Backoffice landing page.

    Clients are the primary entry point (the Client -> Asset -> Work
    hierarchy); the per-business-line tiles and the flat case list stay as
    a fallback. Untriaged cases (no client yet) are surfaced here because
    they are invisible to the hierarchy until triage links them.
    """

    template_name = "backoffice/home.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        open_by_type = dict(
            Case.objects.filter(status__is_terminal=False)
            .values_list("case_type")
            .annotate(n=Count("id"))
        )
        context["client_count"] = Client.objects.count()
        # Newest clients first (no created_at on Client; pk order is a
        # faithful proxy) with their roll-ups for the landing panel.
        context["recent_clients"] = (
            Client.objects.order_by("-pk").annotate(
                asset_count=Count("assets", distinct=True),
                open_case_count=Count(
                    "cases",
                    filter=Q(cases__status__is_terminal=False),
                    distinct=True,
                ),
            )[:8]
        )
        untriaged = Case.objects.filter(
            client__isnull=True, status__is_terminal=False
        ).select_related("status")
        context["untriaged_count"] = untriaged.count()
        context["untriaged_cases"] = untriaged.order_by("-reported_at")[:6]
        # The works someone touched most recently: the list a person
        # coming back to the desk actually wants, and the one thing that
        # tells the landing page apart from the section pages.
        context["recent_cases"] = (
            Case.objects.filter(status__is_terminal=False)
            .select_related("status", "client", "assigned_technician")
            .order_by("-updated_at")[:8]
        )
        context["sections"] = [
            {
                "url_name": "backoffice:section-service",
                "label": _("Service"),
                "open_count": open_by_type.get(CaseType.SERVICE, 0),
            },
            {
                "url_name": "backoffice:section-storage",
                "label": _("Storage"),
                "open_count": open_by_type.get(CaseType.STORAGE, 0),
            },
            {
                "url_name": "backoffice:section-transport",
                "label": _("Transport"),
                "open_count": open_by_type.get(CaseType.TRANSPORT, 0),
            },
        ]
        return context


class ClampedPageMixin:
    """``?page=abc`` -> the first page, ``?page=9999`` -> the last one,
    instead of a 404 (QA UI-L8)."""

    def paginate_queryset(self, queryset, page_size):
        paginator = self.get_paginator(
            queryset,
            page_size,
            orphans=self.get_paginate_orphans(),
            allow_empty_first_page=self.get_allow_empty(),
        )
        number = self.kwargs.get(self.page_kwarg) or self.request.GET.get(
            self.page_kwarg
        )
        if number == "last":
            number = paginator.num_pages
        page = paginator.get_page(number)
        return (paginator, page, page.object_list, page.has_other_pages())


class CaseListView(ClampedPageMixin, StaffRequiredMixin, ListView):
    """Paginated, filterable list of all cases for staff users.

    With ``section_case_type`` set (via ``as_view``) the same view serves
    the per-section pages (Service / Storage / Transport): the
    queryset and the status filter are scoped to that case type and the
    type filter disappears from the UI.
    """

    model = Case
    template_name = "backoffice/case_list.html"
    context_object_name = "cases"
    paginate_by = 25
    ordering = "-created_at"
    section_case_type = None

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related("status", "client", "assigned_technician")
        )
        # Filter by the exact Status row (pk), not by code: workflows of
        # different case types reuse codes/names ("inquiry"/"Upit" exists
        # for TRANSPORT and STORAGE both).
        status_id = self.request.GET.get("status")
        if status_id and status_id.isdigit():
            queryset = queryset.filter(status_id=status_id)
        if self.section_case_type:
            queryset = queryset.filter(case_type=self.section_case_type)
            if self.section_case_type == CaseType.TRANSPORT:
                queryset = queryset.select_related("transport_order")
            elif self.section_case_type == CaseType.STORAGE:
                queryset = queryset.select_related("storage_agreement")
        else:
            case_type = self.request.GET.get("case_type")
            if case_type:
                queryset = queryset.filter(case_type=case_type)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        statuses = Status.objects.order_by("case_type", "sort_order")
        if self.section_case_type:
            statuses = statuses.filter(case_type=self.section_case_type)
        context["statuses"] = statuses
        context["case_type_choices"] = Case._meta.get_field("case_type").choices
        context["current_status"] = self.request.GET.get("status", "")
        context["current_case_type"] = self.request.GET.get("case_type", "")
        context["section"] = self.section_case_type or ""
        context["section_title"] = (
            CaseType(self.section_case_type).label
            if self.section_case_type
            else _("Cases")
        )
        # WNT-01 columns on the storage section: documentation status
        # and the price-list price. Bands are fetched once for the page;
        # storage_agreement/client are select_related - no extra queries.
        if self.section_case_type == CaseType.STORAGE:
            bands = list(StoragePriceBand.objects.all())
            for case in context["cases"]:
                agreement = getattr(case, "storage_agreement", None)
                price = None
                if (
                    agreement is not None
                    and agreement.asset_length_m is not None
                    and agreement.accommodation
                    and agreement.term
                ):
                    length = agreement.asset_length_m
                    band = next(
                        (
                            band
                            for band in bands
                            if band.accommodation == agreement.accommodation
                            and band.length_min_m <= length
                            and (
                                band.length_max_m is None
                                or length < band.length_max_m
                            )
                        ),
                        None,
                    )
                    if band is not None:
                        price = band.price_for_term(agreement.term)
                case.storage_price = price
                has_contact = bool(case.raw_phone or case.raw_email) or (
                    case.client is not None
                    and bool(case.client.phone or case.client.email)
                )
                case.storage_docs_complete = (
                    has_contact
                    and agreement is not None
                    and bool(agreement.permit_no)
                    and agreement.asset_length_m is not None
                )
        return context


class ClientListView(ClampedPageMixin, StaffRequiredMixin, ListView):
    """Client registry browse - the primary client-centric entry point.

    Each row rolls up the client's assets and open work; ``q`` searches
    name, phone and e-mail so staff can find an owner fast.
    """

    model = Client
    template_name = "backoffice/client_list.html"
    context_object_name = "clients"
    paginate_by = 25
    ordering = "name"

    def get_queryset(self):
        queryset = super().get_queryset().annotate(
            asset_count=Count("assets", distinct=True),
            open_case_count=Count(
                "cases",
                filter=Q(cases__status__is_terminal=False),
                distinct=True,
            ),
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(phone__icontains=query)
                | Q(email__icontains=query)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["query"] = self.request.GET.get("q", "")
        return context


class ClientDetailView(StaffRequiredMixin, DetailView):
    """One client with its assets nested, each asset showing its cases.

    Cases triaged to the client but not yet linked to an asset are listed
    separately so nothing is hidden from the hierarchy.
    """

    model = Client
    template_name = "backoffice/client_detail.html"
    context_object_name = "client"

    def get_queryset(self):
        case_qs = Case.objects.select_related("status", "assigned_technician")
        return super().get_queryset().prefetch_related(
            Prefetch("assets", queryset=Asset.objects.order_by("name")),
            Prefetch("assets__cases", queryset=case_qs),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.object
        context["assets"] = client.assets.all()
        context["loose_cases"] = client.cases.filter(
            asset__isnull=True
        ).select_related("status", "assigned_technician")
        return context


class AssetDetailView(StaffRequiredMixin, DetailView):
    """One asset: its cases plus a single gallery aggregating every file
    across all of the asset's work (photos, videos, document scans),
    regardless of which case or child record each file hangs off.
    """

    model = Asset
    template_name = "backoffice/asset_detail.html"
    context_object_name = "asset"

    def get_queryset(self):
        return super().get_queryset().select_related("client")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        asset = self.object
        context["cases"] = asset.cases.select_related(
            "status", "assigned_technician"
        )
        context["asset_media"] = group_attachments_by_kind(
            asset_attachments(asset)
        )
        return context


class CaseCreateView(StaffRequiredMixin, View):
    """Staff intake form for phone/e-mail/walk-in reports (GEN-01).

    On creation the raw contact data is checked against the client
    registry (``suggest_matches``): any hit sends the staff member to
    the triage compare page to explicitly approve linking an existing
    client (or creating a new one) before work continues.
    """

    template_name = "backoffice/case_create.html"

    def get(self, request):
        # The section pages link here with ?case_type=... preselected
        # ("Novi upit" on the storage list); staff may still change it.
        initial = {}
        case_type = request.GET.get("case_type", "")
        if case_type in CaseType.values:
            initial["case_type"] = case_type
        return render(
            request, self.template_name, {"form": CaseCreateForm(initial=initial)}
        )

    def post(self, request):
        form = CaseCreateForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})

        from django.db import transaction

        data = form.cleaned_data
        # One transaction for the case and its storage data: a refused
        # agreement value must not leave a created (and e-mailed) case
        # behind for staff to re-enter as a duplicate.
        try:
            with transaction.atomic():
                case = create_staff_case(
                    case_type=data["case_type"],
                    report_channel=data["report_channel"],
                    contact_name=data["contact_name"],
                    phone=data.get("phone") or "",
                    email=data.get("email") or "",
                    asset_info=data.get("asset_info") or "",
                    location=data.get("location") or "",
                    symptoms_description=data["symptoms_description"],
                    client_diagnosis=(
                        data.get("client_diagnosis") or ""
                        if data["case_type"] == CaseType.SERVICE
                        else ""
                    ),
                    user=request.user,
                )
                # WNT-01: whatever storage documentation the inquiry
                # already brought is persisted on the storage agreement
                # right away; the contracted guard enforces completeness.
                if data["case_type"] == CaseType.STORAGE:
                    storage_fields = {
                        name: data[name]
                        for name in (
                            "permit_no",
                            "asset_length_m",
                            "asset_width_m",
                            "origin_site",
                        )
                        if data.get(name) not in (None, "")
                    }
                    if storage_fields:
                        record_storage_entry(
                            case, user=request.user, **storage_fields
                        )
        except ValidationError as exc:
            form.add_error(None, exc)
            return render(request, self.template_name, {"form": form})
        if suggest_matches(case):
            messages.info(
                request,
                _(
                    "Case #%(id)s created. Possible existing client found - "
                    "review the matches and approve the link."
                ) % {"id": case.pk},
            )
            return redirect("backoffice:case-triage", pk=case.pk)
        messages.success(
            request, _("Case #%(id)s created.") % {"id": case.pk}
        )
        return redirect("backoffice:case-detail", pk=case.pk)


class CaseDetailView(StaffRequiredMixin, DetailView):
    """Staff case detail: full case data plus workflow actions.

    Transition buttons are rendered only for statuses returned by
    ``available_transitions(case, request.user)`` - the UI never offers
    a transition the service would refuse for this user (STM-06).
    """

    model = Case
    template_name = "backoffice/case_detail.html"
    context_object_name = "case"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "status",
                "client",
                "asset",
                "assigned_technician",
                "storage_agreement",
            )
        )

    def _take_invalid_form(self, case):
        """Claim the form the last POST could not accept, if it was ours."""
        stashed = self.request.session.get(INVALID_FORM_SESSION_KEY)
        if not stashed or stashed.get("case_pk") != case.pk:
            return None
        del self.request.session[INVALID_FORM_SESSION_KEY]
        return stashed

    def _form(self, key, form_class, **kwargs):
        """Build a card's form - bound, when this render follows its failure.

        Re-binding beats stashing the error list: validation runs again
        over the same data and reproduces the same errors, so there is no
        second representation of them to keep in step.
        """
        if self._invalid_form and self._invalid_form["key"] == key:
            kwargs["data"] = QueryDict(self._invalid_form["data"])
            form = form_class(**kwargs)
            form.is_valid()
            return form
        return form_class(**kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        case = self.object
        self._invalid_form = self._take_invalid_form(case)
        # Names the section to reopen, so a rejected form is on screen with
        # its errors rather than shut inside a collapsed accordion.
        # Keys may be qualified with a row id ("inspection_form:12") when a
        # card renders one form per record; the anchor is the same either way.
        context["invalid_anchor"] = (
            FORM_ANCHORS.get(self._invalid_form["key"].split(":", 1)[0])
            if self._invalid_form
            else None
        )
        context["transitions"] = available_transitions(case, self.request.user)
        context["status_history"] = case.status_history.select_related(
            "from_status", "to_status", "changed_by"
        )
        context["activity_log"] = case.activity_log.select_related("author")
        context["attachments"] = attachments_for(case).order_by("-created_at")
        # Full media set for the work object: files spread across the case
        # and its child records (work order, intake/handover, delivery),
        # grouped into photos/videos/documents for the gallery.
        context["case_media"] = group_attachments_by_kind(case_attachments(case))
        context["quotes"] = list(
            case.quotes.order_by("-created_at").prefetch_related("approvals")
        )
        # The approval selector offers only the levels the quote still
        # needs (ACT-02): once the client consents, a director-level
        # quote offers Director only - no falling back to Client.
        for quote in context["quotes"]:
            if quote.status == Quote.Status.SENT:
                quote.missing_level_choices = [
                    (level, Approval.Level(level).label)
                    for level in missing_approval_levels(quote)
                ]
                quote.advance_form = self._form(
                    f"advance_form:{quote.pk}", QuoteAdvanceForm
                )
        # An open (draft/sent) quote parks the create form behind a
        # "new revision" toggle so the card doesn't invite duplicates;
        # rejected/accepted quotes free the form up again.
        context["active_quote"] = next(
            (
                quote
                for quote in context["quotes"]
                if quote.status in (Quote.Status.DRAFT, Quote.Status.SENT)
            ),
            None,
        )
        context["part_orders"] = case.part_orders.select_related(
            "part", "advance_payment"
        ).order_by("-ordered_at")
        context["part_order_form"] = self._form("part_order_form", PartOrderCreateForm)
        context["advance_payments"] = case.advance_payments.select_related(
            "quote"
        ).order_by("-pk")
        context["approval_decisions"] = Approval.Decision.choices
        # GEN-03: a portal case keeps its raw_* audit fields; until a staff
        # member links a Client, the detail page points to the triage view.
        context["needs_triage"] = case.client_id is None and any(
            (
                case.raw_contact_name,
                case.raw_phone,
                case.raw_email,
                case.raw_asset_info,
            )
        )
        # SVC-18 / TRP-09 / INV-07: in the pre-closing status of every
        # case type, surface the intake-vs-handover checklist and the
        # closing checklist so staff can confirm discrepancies and
        # settlement before closing.
        # SVC-03: a case worked on site or in the field has no
        # handover - the comparison it renders is against an intake record
        # that was never written, and the guard no longer asks for one.
        # Two flags, because the two cards answer to different rules. The
        # closing checklist belongs to every case in a pre-closing status:
        # SVC-19 still asks for financial settlement on an on-site job, and
        # gating its toggle on requires_asset_intake left such a case with
        # no way to be settled - and therefore no way to be closed.
        # §7.2: the six asset data, editable wherever the case is.
        if case.asset is not None:
            context["asset_identity_form"] = self._form(
                "asset_identity_form",
                AssetIdentityForm,
                instance=case.asset,
            )
            context["asset_missing_identification"] = (
                case.asset.missing_identification()
            )
        # §7.1: who we ring. Offered on every case type - winter
        # storage names the same datum ("record of ... contact") and a
        # transport job is agreed with somebody too - while only the
        # SERVICE gate enforces it, because ``intake_scheduled`` is a
        # SERVICE status. REPORTER is preselected on a fresh case: it is
        # the answer nearly always, and the click is the confirmation.
        context["contact_person_form"] = self._form(
            "contact_person_form",
            ContactPersonForm,
            initial={
                "role": case.contact_role or ContactRole.REPORTER,
                "name": case.contact_name,
                "phone": case.contact_phone,
                "email": case.contact_email,
            },
        )
        context["contact_details"] = case.contact_details()
        # SVC-01 / §7.4: the fault report card is a service-only
        # question and stays on screen until the case is closed, because
        # a client who calls back with a theory is ordinary.
        if case.case_type == CaseType.SERVICE:
            context["report_location_form"] = self._form(
                "report_location_form",
                ReportLocationForm,
                initial={"location": case.location},
            )
            context["client_assessment_form"] = self._form(
                "client_assessment_form",
                ClientAssessmentForm,
                initial={
                    "client_diagnosis": case.client_diagnosis,
                    "gave_no_assessment": case.client_gave_no_assessment,
                },
            )
        context["awaiting_closing"] = case.status.code in (
            "ready_for_handover",
            "delivered",
            "ready_for_pickup",
        )
        context["awaiting_handover"] = (
            case.requires_asset_intake and context["awaiting_closing"]
        )
        if context["awaiting_handover"]:
            handover = case.handover_records.order_by("-created_at", "-pk").first()
            context["handover"] = handover
            if handover is not None:
                context["handover_comparison"] = compare_handover_with_intake(
                    handover
                )
                # SVC-18: the client confirmation is gated on the worker
                # signature and the client-approval scan.
                context["handover_has_approval"] = has_handover_approval(handover)
                # §7/SVC-07: photographed on the way out as well as in.
                context["handover_has_photos"] = has_handover_photos(handover)
                context["handover_media_form"] = self._form(
                    "handover_media_form", HandoverMediaUploadForm
                )
                context["handover_approval_form"] = self._form(
                    "handover_approval_form", HandoverApprovalUploadForm
                )
                context["handover_sign_form"] = self._form(
                    "handover_sign_form",
                    HandoverSignForm,
                    initial={"discrepancies_note": handover.discrepancies_note},
                )
        # SVC-19: once closed, name the manager/director whose transition
        # closed the case (the closing signature is the StatusHistory row).
        if case.status.code == "closed":
            context["closed_entry"] = (
                case.status_history.filter(to_status__code="closed")
                .select_related("changed_by")
                .order_by("-changed_at", "-pk")
                .first()
            )
        user = self.request.user
        context["can_settle"] = user.is_superuser or user.role in SETTLEMENT_ROLES
        # SVC-20: reopening a closed order is the same management act.
        context["work_order_reopen_form"] = self._form(
            "work_order_reopen_form", WorkOrderReopenForm
        )
        # SVC-10 / QA SV-H4: only the catalogue's owners may declare a
        # part a stocked consumable; everyone else adds an ordinary part.
        context["can_stock_parts"] = _may_edit_pricing(user)
        context["part_quick_add_form"] = self._form(
            "part_quick_add_form", PartQuickAddForm
        )
        # Forms + related objects for the action cards. activity_form was
        # referenced by the template but never provided - fixed here.
        context["activity_form"] = self._form("activity_form", ActivityLogForm)
        context["work_location_form"] = self._form(
            "work_location_form",
            WorkLocationForm,
            initial={"work_location": case.work_location},
        )
        # SVC-04: the other half of what "Intake scheduled" means. It sits
        # beside the work-location consult because they are the two
        # pre-arrival answers, and this one only applies when the first
        # says the asset is coming in at all.
        context["delivery_method_form"] = self._form(
            "delivery_method_form",
            DeliveryMethodForm,
            initial={"delivery_method": case.delivery_method},
        )
        context["appointment_form"] = self._form("appointment_form", AppointmentBookForm)
        context["intake_form"] = self._form("intake_form", IntakeRecordCreateForm)
        context["intake_item_form"] = self._form("intake_item_form", IntakeItemForm)
        context["intake_amendment_form"] = self._form(
            "intake_amendment_form", IntakeAmendmentForm
        )
        context["attachment_form"] = self._form("attachment_form", AttachmentUploadForm)
        # WNT-01/WNT-02/WNT-06: the storage documentation, asset-link
        # and storage-position cards; the price-list suggestion prefills
        # the quote form (OPEN-05) but stays staff-editable.
        quote_initial = {}
        context["storage_agreement"] = None
        if case.case_type == CaseType.STORAGE:
            agreement = getattr(case, "storage_agreement", None)
            context["storage_agreement"] = agreement
            context["storage_docs_form"] = self._form(
                "storage_docs_form", StorageAgreementForm, instance=agreement
            )
            suggested = price_for(agreement) if agreement is not None else None
            context["storage_suggested_price"] = suggested
            context["storage_positions"] = (
                list(agreement.positions.all()) if agreement is not None else []
            )
            context["storage_position_form"] = self._form(
                "storage_position_form", StoragePositionAssignForm
            )
            # WNT rule 4: the site-pickup card (organizer, lift-out,
            # delivery); the receipt/inventory/photos are the intake stack.
            pickup = (
                getattr(agreement, "site_pickup", None)
                if agreement is not None
                else None
            )
            context["site_pickup"] = pickup
            context["site_pickup_form"] = self._form(
                "site_pickup_form", SitePickupForm, instance=pickup
            )
            if suggested is not None:
                quote_initial = {
                    "amount": suggested,
                    "description": _(
                        "Storage - asset %(length)s m, "
                        "%(accommodation)s, %(term)s (price-list suggestion)"
                    ) % {
                        "length": agreement.asset_length_m,
                        "accommodation": agreement.get_accommodation_display(),
                        "term": agreement.get_term_display(),
                    },
                }
            # WNT-03: the offer card - an existing package user is
            # suggested a renewal, a new client gets the package as the
            # recommended option; everything stays staff-editable.
            offer = suggest_offer(case)
            context["storage_offer"] = offer
            context["storage_offer_form"] = self._form("storage_offer_form", StorageOfferForm,
                initial={
                    "offer_type": "package"
                    if offer["package"] is not None
                    else "standalone",
                    "package": offer["package"],
                    "period_start": offer["period_start"],
                    "period_end": offer["period_end"],
                    "description": offer["description"]
                    or quote_initial.get("description", ""),
                    "amount": offer["amount"] or quote_initial.get("amount"),
                }
            )
        context["quote_form"] = self._form("quote_form", QuoteCreateForm,
            initial=quote_initial,
            with_catalog=case.case_type == CaseType.SERVICE,
        )
        context["appointments"] = case.appointments.all()
        context["work_order"] = getattr(case, "work_order", None)
        # SVC-05: the client's approval is the scanned document on the
        # work order; the sign button is for the responsible technician.
        context["work_order_has_approval"] = context[
            "work_order"
        ] is not None and has_client_approval(context["work_order"])
        context["work_order_approval_form"] = self._form(
            "work_order_approval_form", WorkOrderApprovalUploadForm
        )
        # SVC-02: on a service case the work order precedes the intake
        # record, so the create form stays hidden until one is opened.
        context["intake_needs_work_order"] = (
            case.case_type == CaseType.SERVICE and context["work_order"] is None
        )
        # The diagnostics card is SERVICE-only in the template; the form
        # loads the whole parts catalogue, so do not build it for the
        # storage and transport renders that never show it.
        context["diagnostic_form"] = (
            self._form("diagnostic_form", DiagnosticCreateForm)
            if case.case_type == CaseType.SERVICE
            else None
        )
        context["diagnostics"] = (
            context["work_order"].diagnostics.order_by("created_at")
            if context["work_order"] is not None
            else []
        )
        # SVC-15/SVC-16/SVC-17: execution log, quality control and the
        # client completion notice.
        work_order = context["work_order"]
        if work_order is not None:
            context["work_log"] = work_order.work_log.select_related(
                "performed_by"
            ).order_by("created_at")
            context["inspection_record"] = (
                InspectionRecord.objects.filter(work_order=work_order)
                .prefetch_related("items__point")
                .first()
            )
            context["used_parts"] = work_order.used_parts.select_related(
                "part"
            ).order_by("pk")
            context["total_hours"] = sum(
                (entry.hours for entry in context["work_log"]), Decimal("0")
            )
            context["test_runs"] = work_order.test_runs.order_by(
                "-performed_at", "-pk"
            )
        else:
            context["work_log"] = []
            context["inspection_record"] = None
            context["used_parts"] = []
            context["total_hours"] = Decimal("0")
            context["test_runs"] = []
        context["work_log_form"] = self._form("work_log_form", WorkLogEntryForm)
        context["is_new_client"] = is_new_client(case)
        # One form for the whole checklist (SVC-08). It was one form per
        # row, each with its own Save - dozens of round trips for a
        # first-time client's inspection. The rows are the form's own
        # fields, so a rejected POST re-binds through the same slot as
        # every other card and lands its errors on the offending row.
        record = context["inspection_record"]
        context["inspection_form"] = (
            self._form(
                "inspection_form",
                InspectionResultsForm,
                items=list(record.items.select_related("point")),
            )
            if record is not None and not record.signed_at
            else None
        )
        context["used_part_form"] = self._form("used_part_form", UsedPartForm)
        context["work_docs_form"] = self._form("work_docs_form", WorkDocsUploadForm)
        context["test_run_form"] = self._form(
            "test_run_form", TestRunForm, case=case
        )
        # SVC-17 (procedure §4.3): the client is told the *final* cost, so
        # the field starts at what the work order actually records rather
        # than empty. When it cannot be totalled the field stays blank and
        # the service asks for the reason instead - a guess prefilled here
        # would be the very thing the rule exists to stop.
        try:
            recorded_cost = work_cost_so_far(case)
        except CostUnknown as exc:
            recorded_cost = None
            context["completion_cost_problem"] = "; ".join(exc.messages)
        context["completion_recorded_cost"] = recorded_cost
        context["completion_notice_form"] = self._form(
            "completion_notice_form",
            CompletionNoticeForm,
            initial={"final_cost": recorded_cost} if recorded_cost else None,
        )
        context["completion_notices"] = case.completion_notices.select_related(
            "created_by"
        )
        context["intake_records"] = case.intake_records.order_by(
            "-created_at", "-pk"
        ).prefetch_related("items")
        # SVC-06 UI: the create form is hidden while an unsigned record is
        # open (the server would reject a second one anyway) and collapsed
        # behind an explicit button once every record is signed.
        context["intake_open_record"] = next(
            (r for r in context["intake_records"] if r.signed_at is None),
            None,
        )
        context["transport_order"] = getattr(case, "transport_order", None)
        # TRP-03: the asset length recommends a mode, and an asset own
        # transport cannot handle (over ~10 m) needs a declared carrier price.
        from apps.transport.models import TransportOrder as _TransportOrder

        _asset = getattr(case, "asset", None)
        _rec_mode = (
            recommended_mode(_asset.length_m) if _asset is not None else None
        )
        context["transport_recommended_mode_label"] = (
            _TransportOrder.Mode(_rec_mode).label if _rec_mode else None
        )
        context["transport_needs_carrier_price"] = bool(
            context["transport_order"]
            and _rec_mode == _TransportOrder.Mode.EXTERNAL_CARRIER
            and context["transport_order"].carrier_price_declared_at is None
        )
        # TRP-02: the agreed loading/unloading plan and its form.
        loading_plan = (
            getattr(context["transport_order"], "loading_plan", None)
            if context["transport_order"]
            else None
        )
        context["loading_plan"] = loading_plan
        context["loading_plan_form"] = self._form(
            "loading_plan_form", LoadingPlanForm, instance=loading_plan
        )
        context["loading_plan_missing"] = (
            missing_plan_items(loading_plan)
            if context["transport_order"]
            else []
        )
        # TRP-07: the internal loading confirmation and its form.
        loading_confirmation = (
            getattr(context["transport_order"], "loading_confirmation", None)
            if context["transport_order"]
            else None
        )
        context["loading_confirmation"] = loading_confirmation
        loading_initial = {}
        if (
            loading_confirmation is None or not loading_confirmation.carrier
        ) and context["transport_order"] is not None:
            # TRP-07: the carrier verifying the securing is the carrier
            # TRP-03 already named and priced - retyping it is how it ends
            # up blank on the one record that should show two parties.
            loading_initial["carrier"] = (
                context["transport_order"].carrier_name or ""
            )
        context["loading_confirmation_form"] = self._form(
            "loading_confirmation_form", LoadingConfirmationForm,
            instance=loading_confirmation, initial=loading_initial
        )
        # TRP-08: the delivery-protocol card and its forms. The planned
        # attendee (TRP-02) prefills the actual attendee when unset.
        delivery_confirmation = (
            getattr(context["transport_order"], "delivery_confirmation", None)
            if context["transport_order"]
            else None
        )
        context["delivery_confirmation"] = delivery_confirmation
        delivery_initial = {}
        if delivery_confirmation is None or not delivery_confirmation.attendee_name:
            loading_plan = (
                getattr(context["transport_order"], "loading_plan", None)
                if context["transport_order"]
                else None
            )
            if loading_plan is not None and loading_plan.attendee:
                delivery_initial["attendee_name"] = loading_plan.attendee
        context["delivery_confirmation_form"] = self._form(
            "delivery_confirmation_form", DeliveryConfirmationForm,
            instance=delivery_confirmation, initial=delivery_initial
        )
        context["delivery_has_protocol"] = (
            delivery_confirmation is not None
            and has_delivery_protocol(delivery_confirmation)
        )
        context["carrier_price_form"] = self._form(
            "carrier_price_form", TransportCarrierPriceForm
        )
        context["delivery_protocol_form"] = self._form(
            "delivery_protocol_form", DeliveryProtocolUploadForm
        )
        # TRP-01: the offer card - scope of service prefilled from the
        # standard price list scope, amount from the estimate or the price
        # matrix; everything stays staff-editable.
        if case.case_type == CaseType.TRANSPORT:
            from apps.backoffice.transport import (
                TransportOfferForm,
                transport_offer_prefill,
            )

            transport_offer = transport_offer_prefill(case)
            context["transport_offer"] = transport_offer
            context["transport_offer_form"] = self._form("transport_offer_form", TransportOfferForm,
                initial={
                    "description": transport_offer["description"],
                    "amount": transport_offer["amount"],
                }
            )
        # Guided workflow (wizard): reuses everything fetched above.
        from apps.backoffice.guide import GuideContext, build_guide

        guide_ctx = GuideContext(
            case,
            appointments=list(context["appointments"]),
            work_order=context["work_order"],
            intake_records=list(context["intake_records"]),
            attachments=list(context["attachments"]),
            quotes=list(context["quotes"]),
            advance_payments=list(context["advance_payments"]),
            part_orders=list(context["part_orders"]),
            handover=context.get("handover"),
            transport_order=context["transport_order"],
        )
        context["guide"] = build_guide(case, user, guide_ctx)
        return context


def _report_auto_advance(request, advanced):
    """Surface an info message when :func:`auto_advance_case` moved the
    case forward along one or more linear steps, so staff see where it
    landed without the wizard being an interactive control."""
    if not advanced:
        return
    messages.info(
        request,
        _("Advanced automatically to '%(status)s'.")
        % {"status": advanced[-1].name},
    )


class CaseTransitionView(StaffRequiredMixin, View):
    """POST target for the transition buttons on the case detail page.

    Delegates to ``transition_case()`` (STM-02); any workflow refusal
    (invalid edge, failed guard, disallowed role) is surfaced to the
    staff user via a message naming the missing precondition (STM-04).
    """

    def post(self, request, pk):
        case = get_object_or_404(Case, pk=pk)
        to_status_code = request.POST.get("to_status", "")
        note = request.POST.get("note", "")
        try:
            transition_case(case, to_status_code, request.user, note=note)
        except CaseWorkflowError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                _("Case moved to '%(status)s'.") % {"status": case.status.name},
            )
            # A chosen branch may be followed by linear steps: chain them.
            _report_auto_advance(request, auto_advance_case(case, request.user))
        return redirect(reverse("backoffice:case-detail", args=[case.pk]))


class CaseActivityLogView(StaffRequiredMixin, View):
    """POST target for the inline activity-log form (GEN-02).

    Creates an append-only ``ActivityLog`` entry authored by the
    logged-in staff user; phone agreements affecting price, scope or
    deadline are documented this way.
    """

    def post(self, request, pk):
        case = get_object_or_404(Case, pk=pk)
        form = ActivityLogForm(request.POST)
        # INV-15 first: a closed case gets the refusal, not field errors
        # about a note it would never take (the service refuses too).
        refusal = closed_case_message(case)
        if refusal:
            messages.error(request, refusal)
        elif form.is_valid():
            try:
                add_activity_entry(
                    case,
                    request.user,
                    form.cleaned_data["kind"],
                    form.cleaned_data["text"],
                )
            except ValidationError as exc:  # INV-15
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, _("Activity log entry added."))
        else:
            errors = "; ".join(
                e for field_errors in form.errors.values() for e in field_errors
            )
            messages.error(
                request,
                _("Activity log entry not saved: %(errors)s") % {"errors": errors},
            )
        return redirect(reverse("backoffice:case-detail", args=[case.pk]))


#: Where a failed form's data waits out the redirect. One slot: a POST
#: carries one form, and the next case-detail render consumes it.
INVALID_FORM_SESSION_KEY = "backoffice_invalid_form"

#: Which collapsible section holds each form, so the redirect can reopen
#: the one the user was working in instead of dropping them at the top of
#: a page whose sections are all shut.
FORM_ANCHORS = {
    "activity_form": "acc-activity",
    "work_order_reopen_form": "acc-closing",
    "part_quick_add_form": "acc-diagnostics",
    "advance_form": "acc-quotes",
    "appointment_form": "acc-appointments",
    "attachment_form": "acc-attachments",
    "completion_notice_form": "acc-completion",
    "carrier_price_form": "acc-transport",
    "delivery_confirmation_form": "acc-transport",
    "delivery_method_form": "acc-work-location",
    "delivery_protocol_form": "acc-transport",
    "diagnostic_form": "acc-diagnostics",
    "handover_approval_form": "acc-handover",
    "handover_media_form": "acc-handover",
    "client_assessment_form": "acc-fault-report",
    "report_location_form": "acc-fault-report",
    "contact_person_form": "acc-contact-person",
    "asset_identity_form": "acc-asset-identity",
    "handover_sign_form": "acc-handover",
    "intake_form": "acc-intake",
    "inspection_form": "acc-inspection",
    "intake_item_form": "acc-intake",
    "intake_amendment_form": "acc-intake",
    "loading_confirmation_form": "acc-transport",
    "loading_plan_form": "acc-transport",
    "work_location_form": "acc-work-location",
    "site_pickup_form": "acc-site-pickup",
    "part_order_form": "acc-part-orders",
    "quote_form": "acc-quotes",
    "storage_docs_form": "acc-storage-docs",
    "storage_offer_form": "acc-quotes",
    "storage_position_form": "acc-storage-position",
    "test_run_form": "acc-quality",
    "transport_offer_form": "acc-transport",
    "used_part_form": "acc-work-execution",
    "work_docs_form": "acc-work-execution",
    "work_log_form": "acc-work-execution",
    "work_order_approval_form": "acc-work-order",
}


class FormInvalid(Exception):
    """A bound form failed, and the user must get it back with its errors.

    Every action here answers a POST with a redirect, so the bound form
    would die with the request - which is why the old
    ``raise ValidationError(form.errors.as_text())`` threw the user's
    input away and flashed a blob naming raw field names
    ("* asset_length_m"). Raising this instead hands the redirect the
    form's context key and what the user typed; ``CaseDetailView``
    re-binds it on the way back in, so re-validation reproduces the same
    errors and they land on the fields that caused them.

    Uploaded files cannot survive a redirect and are not stashed: a file
    field comes back empty while the text beside it is preserved. That is
    still strictly more than the nothing that survived before.
    """

    def __init__(self, form_key, form):
        self.form_key = form_key
        self.form = form
        super().__init__(form_key)


def stash_invalid_form(request, case, exc):
    request.session[INVALID_FORM_SESSION_KEY] = {
        "key": exc.form_key,
        "case_pk": case.pk,
        "data": request.POST.urlencode(),
    }


class CaseActionView(StaffRequiredMixin, View):
    """Base for POST-only actions that call a domain service.

    Subclasses implement ``perform(request, case)`` and either return a
    success message or raise. The user is redirected back to the case
    detail page either way; what differs is how the failure gets there.
    Raise ``FormInvalid`` when a form failed validation - the form comes
    back bound, with its errors on its fields. ``ValidationError`` is for
    refusals that belong to no field (a guard, a missing prerequisite) and
    is surfaced via ``django.contrib.messages``.

    A case in a terminal status takes no further actions (INV-15). The
    state machine already refuses to move a closed case anywhere, so
    "closed" is permanent; without this, everything that does not go
    through ``transition_case`` stayed writable forever, and a closed
    case could still be given a work order, an intake record, a quote or an
    appointment. The case file is the record of what happened, and the
    record stops when the case does. Subclasses opt out deliberately by
    setting ``allowed_when_closed``.
    """

    #: Whether this action still applies once the case is closed. The
    #: default is no. Actions that only add documentation to the archive
    #: (and cannot change any guard's answer, since a closed case has no
    #: transitions left) may set it.
    allowed_when_closed = False

    def _require_open_case(self, case):
        if self.allowed_when_closed:
            return
        message = closed_case_message(case)
        if message is not None:
            raise ValidationError(message)

    def post(self, request, pk, **kwargs):
        case = get_object_or_404(Case, pk=pk)
        try:
            self._require_open_case(case)
            success = self.perform(request, case, **kwargs)
        except FormInvalid as exc:
            stash_invalid_form(request, case, exc)
            messages.error(request, _("Please correct the highlighted fields."))
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        except CaseWorkflowError as exc:
            # A FAIL test run moves the case back itself; a refused edge
            # (an admin-restricted role, say) is a message, not a 500.
            messages.error(request, str(exc))
        else:
            messages.success(request, success)
            _report_auto_advance(request, auto_advance_case(case, request.user))
        return redirect(reverse("backoffice:case-detail", args=[case.pk]))


class QuoteSendView(CaseActionView):
    """Send a DRAFT quote (SVC-12); the client email follows from the
    ``awaiting_approval`` transition, not from here (NTF-03)."""

    def perform(self, request, case, quote_pk):
        quote = get_object_or_404(Quote, pk=quote_pk, case=case)
        send_quote(quote, request.user)
        # TRP-04: on a transport case the mandatory 50% advance is derived
        # from the offer that was just sent - staff only confirm payment.
        advance = ensure_transport_advance(case, user=request.user)
        if advance is not None and advance.paid_at is None:
            return _(
                "Quote #%(id)s sent; 50%% advance of %(amount)s %(currency)s "
                "recorded (advance #%(advance)s) - confirm it once paid."
            ) % {
                "id": quote.pk,
                "amount": advance.amount,
                "currency": quote.currency,
                "advance": advance.pk,
            }
        return _("Quote #%(id)s sent.") % {"id": quote.pk}


class QuoteApprovalView(CaseActionView):
    """Record an approval decision on a quote (ACT-02, SVC-12, INV-04)."""

    def perform(self, request, case, quote_pk):
        quote = get_object_or_404(Quote, pk=quote_pk, case=case)
        level = request.POST.get("level", "")
        decision = request.POST.get("decision", "")
        if level not in Approval.Level.values:
            raise ValidationError(
                _("Unknown approval level: %(level)s.") % {"level": level or "-"}
            )
        if decision not in Approval.Decision.values:
            raise ValidationError(
                _("Unknown decision: %(decision)s.") % {"decision": decision or "-"}
            )
        record_approval(
            quote,
            level,
            decision,
            user=request.user,
            note=request.POST.get("note", ""),
        )
        quote.refresh_from_db()
        return _(
            "Recorded %(level)s %(decision)s on quote #%(id)s (quote is now %(status)s)."
        ) % {
            "level": level,
            "decision": decision,
            "id": quote.pk,
            "status": quote.get_status_display(),
        }


class QuoteAdvanceView(CaseActionView):
    """Request an advance payment against a quote (SVC-11)."""

    def perform(self, request, case, quote_pk):
        quote = get_object_or_404(Quote, pk=quote_pk, case=case)
        # TRP-04: a transport advance is derived from the offer, never
        # typed. The form is hidden there, but the endpoint took a POST
        # with percent=1 and the case then scheduled on it (QA T-H2).
        if case.case_type == CaseType.TRANSPORT:
            raise ValidationError(
                _(
                    "The transport advance is derived from the offer (50%) "
                    "and cannot be requested by hand (TRP-04)."
                )
            )
        form = QuoteAdvanceForm(request.POST)
        if not form.is_valid():
            raise FormInvalid(f"advance_form:{quote_pk}", form)
        percent = form.cleaned_data["percent"]
        advance = request_advance_payment(case, quote=quote, percent=percent)
        return _(
            "Advance payment #%(id)s requested: %(percent)s%% of quote "
            "#%(quote)s (%(amount)s %(currency)s)."
        ) % {
            "id": advance.pk,
            "percent": percent,
            "quote": quote.pk,
            "amount": advance.amount,
            "currency": quote.currency,
        }


class PartOrderCreateView(CaseActionView):
    """Order a part from the case detail page (SVC-10/SVC-11).

    Staff pick the part and type its price freely; for a non-stock part
    the mandatory 50% advance (INV-05) is computed from that price and
    recorded on the case automatically, then linked to the order - no
    hand-built advance object in the admin.
    """

    def perform(self, request, case):
        form = PartOrderCreateForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("part_order_form", form)
        order, advance = order_part_with_advance(
            case,
            form.cleaned_data["part"],
            form.cleaned_data["price"],
            expected_delivery=form.cleaned_data["expected_delivery"],
        )
        if advance is not None:
            return _(
                "Part order #%(id)s created; 50%% advance of %(amount)s € "
                "recorded on the case (advance #%(advance)s)."
            ) % {"id": order.pk, "amount": advance.amount, "advance": advance.pk}
        return _(
            "Part order #%(id)s created (stocked consumable, no advance "
            "required)."
        ) % {"id": order.pk}


class PartOrderReceiveView(CaseActionView):
    """Mark an open part order as received (stock +1, SYSTEM log)."""

    def perform(self, request, case, order_pk):
        order = get_object_or_404(PartOrder, pk=order_pk, case=case)
        receive_part_order(order, request.user)
        return _("Part order #%(id)s marked received.") % {"id": order.pk}


class HandoverCreateView(CaseActionView):
    """Create the handover record for the SVC-18 comparison."""

    def perform(self, request, case):
        handover = create_handover_record(
            case, request.user, request.POST.get("discrepancies_note", "")
        )
        return _("Handover record #%(id)s created.") % {"id": handover.pk}


class HandoverNoteView(CaseActionView):
    """Save discrepancies noted during the comparison (SVC-18)."""

    def perform(self, request, case, handover_pk):
        handover = get_object_or_404(HandoverRecord, pk=handover_pk, case=case)
        update_handover_note(handover, request.POST.get("discrepancies_note", ""))
        return _("Discrepancies note saved on handover record #%(id)s.") % {
            "id": handover.pk
        }


class HandoverSignView(CaseActionView):
    """Capture the receiving signature as name + timestamp (SVC-18)."""

    def perform(self, request, case, handover_pk):
        handover = get_object_or_404(HandoverRecord, pk=handover_pk, case=case)
        form = HandoverSignForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("handover_sign_form", form)
        received_by_name = form.cleaned_data["received_by_name"]
        # Only an actually-submitted note counts. cleaned_data hands back ""
        # for a field the form did not carry, and writing that through would
        # erase a note recorded earlier - the sign form does not render the
        # note field, so every signature would wipe it.
        if "discrepancies_note" in request.POST:
            note = form.cleaned_data["discrepancies_note"]
            if note != handover.discrepancies_note and handover.signed_at is None:
                update_handover_note(handover, note)
        sign_handover(handover, received_by_name)
        return _(
            "Handover record #%(id)s signed; receipt confirmed by '%(name)s'."
        ) % {"id": handover.pk, "name": received_by_name}


class FinancialSettlementView(CaseActionView):
    """Toggle the financial-settlement half of the closing guard
    (SVC-19, MANAGER/DIRECTOR only)."""

    def perform(self, request, case):
        settled = request.POST.get("settled") == "1"
        set_financial_settlement(
            case, request.user, settled, note=request.POST.get("note", "")
        )
        state = (
            _("financially settled") if settled else _("not financially settled")
        )
        return _("Case #%(id)s marked as %(state)s.") % {
            "id": case.pk,
            "state": state,
        }


class WorkOrderReopenView(CaseActionView):
    """SVC-20: reopen a closed work order so a correction can be
    recorded (MANAGER/DIRECTOR only, and the reason is kept)."""

    def perform(self, request, case):
        work_order = _work_order_or_error(case)
        form = WorkOrderReopenForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("work_order_reopen_form", form)
        reopen_work_order(
            work_order, request.user, reason=form.cleaned_data["reason"]
        )
        return _("Work order #%(id)s reopened.") % {"id": work_order.pk}


class WorkLocationView(CaseActionView):
    """SVC-03: record the pre-arrival consult on where the work is done."""

    def perform(self, request, case):
        form = WorkLocationForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("work_location_form", form)
        record_work_location_consult(
            case,
            form.cleaned_data["work_location"],
            request.user,
            note=form.cleaned_data["note"],
            consulted=form.cleaned_data["consulted"],
        )
        return _("Work location recorded: %(location)s.") % {
            "location": case.get_work_location_display()
        }


class AssetIdentityView(CaseActionView):
    """§7.2: complete the asset's identification from the case page."""

    def perform(self, request, case):
        if case.asset is None:
            raise ValidationError(
                _(
                    "This case has no asset record yet - match or create "
                    "one at triage first (GEN-03)."
                )
            )
        form = AssetIdentityForm(request.POST, instance=case.asset)
        if not form.is_valid():
            raise FormInvalid("asset_identity_form", form)
        # A valid ModelForm has already written the posted values onto
        # its instance (``_post_clean`` calls ``construct_instance``), so
        # ``case.asset`` is no longer the row as stored. The service
        # compares old against new to decide what changed and what to
        # log; handed the mutated instance it would find every field
        # equal to itself and save nothing at all. Re-read the row.
        asset = Asset.objects.get(pk=case.asset_id)
        record_asset_identification(
            asset,
            request.user,
            case=case,
            **{name: form.cleaned_data[name] for name in form.Meta.fields}
        )
        return _("Asset identification updated.")


class ContactPersonView(CaseActionView):
    """§7.1: designate who we contact about this case."""

    def perform(self, request, case):
        form = ContactPersonForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("contact_person_form", form)
        record_contact_person(
            case,
            request.user,
            role=form.cleaned_data["role"],
            name=form.cleaned_data["name"],
            phone=form.cleaned_data["phone"],
            email=form.cleaned_data["email"],
        )
        name, phone, email = case.contact_details()
        return _("Contact person recorded: %(role)s (%(name)s).") % {
            "role": case.get_contact_role_display(),
            "name": name or phone or email or "-",
        }


class ReportLocationView(CaseActionView):
    """§7.3: record or correct where the asset is."""

    def perform(self, request, case):
        form = ReportLocationForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("report_location_form", form)
        record_report_location(case, form.cleaned_data["location"], request.user)
        return _("Location recorded: %(location)s.") % {
            "location": case.location
        }


class ClientAssessmentView(CaseActionView):
    """SVC-01: record what the client thinks is wrong (§7.4)."""

    def perform(self, request, case):
        form = ClientAssessmentForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("client_assessment_form", form)
        record_client_assessment(
            case,
            request.user,
            assessment=form.cleaned_data["client_diagnosis"],
            gave_no_assessment=form.cleaned_data["gave_no_assessment"],
        )
        if case.client_gave_no_assessment:
            return _("Recorded: the client had no view of what is wrong.")
        return _("The client's assessment of the fault was recorded.")


class DeliveryMethodView(CaseActionView):
    """SVC-04: record how the asset reaches the yard."""

    def perform(self, request, case):
        form = DeliveryMethodForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("delivery_method_form", form)
        record_delivery_method(
            case,
            form.cleaned_data["delivery_method"],
            request.user,
            note=form.cleaned_data["note"],
        )
        return _("Delivery method recorded: %(method)s.") % {
            "method": case.get_delivery_method_display()
        }


class AppointmentBookView(CaseActionView):
    """Book an appointment on the case (SVC-04)."""

    def perform(self, request, case):
        form = AppointmentBookForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("appointment_form", form)
        appointment = book_appointment(
            case,
            form.cleaned_data["kind"],
            form.cleaned_data["scheduled_for"],
            request.user,
            note=form.cleaned_data["note"],
        )
        return _("Appointment #%(id)s booked for %(when)s.") % {
            "id": appointment.pk,
            "when": appointment.scheduled_for.strftime("%d.%m.%Y %H:%M"),
        }


class WorkOrderCreateView(CaseActionView):
    """Open the (preliminary) work order (SVC-02)."""

    def perform(self, request, case):
        work_order = create_work_order(case, request.user)
        return _("Work order #%(id)s opened.") % {"id": work_order.pk}


class DiagnosticCreateView(CaseActionView):
    """Record the diagnostic on the work order (SVC-09)."""

    def perform(self, request, case):
        work_order = getattr(case, "work_order", None)
        if work_order is None:
            raise ValidationError(
                _("Case #%(id)s has no work order yet.") % {"id": case.pk}
            )
        form = DiagnosticCreateForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("diagnostic_form", form)
        diagnostic = record_diagnostic(
            work_order,
            request.user,
            issue=form.cleaned_data["issue"],
            servicing_work=form.cleaned_data["servicing_work"],
            parts_needed=form.cleaned_data["parts_needed"],
            needed_parts=form.needed_part_rows(),
            estimated_parts_price=form.cleaned_data["estimated_parts_price"],
            estimated_completion_date=form.cleaned_data["estimated_completion_date"],
        )
        return _("Diagnostic #%(id)s recorded on work order #%(order)s.") % {
            "id": diagnostic.pk,
            "order": work_order.pk,
        }


class PartQuickAddView(CaseActionView):
    """SVC-10: mint a catalogue row for a part the diagnosis had to
    describe in prose, so the rest of the chain can reference it."""

    def perform(self, request, case):
        form = PartQuickAddForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("part_quick_add_form", form)
        # Who may declare the part a stocked consumable is the service's
        # rule (``may_stock_parts``); others still add it as an ordinary one.
        part = create_catalogue_part(
            case,
            request.user,
            code=form.cleaned_data["code"],
            name=form.cleaned_data["name"],
            is_consumable=form.cleaned_data["is_consumable"],
            stock_qty=form.cleaned_data["stock_qty"],
            unit_price=form.cleaned_data["unit_price"],
        )
        return _(
            "Part '%(part)s' added to the catalogue - it can be selected on "
            "the next diagnostic."
        ) % {"part": part}


class InspectionStartView(CaseActionView):
    """Open the new-client full inspection on the work order (SVC-08)."""

    def perform(self, request, case):
        work_order = _work_order_or_error(case)
        record = start_inspection(work_order, request.user)
        return _("Full inspection #%(id)s opened.") % {"id": record.pk}


class InspectionResultsView(CaseActionView):
    """Record the findings for every point of the inspection at once (SVC-08)."""

    def perform(self, request, case, record_pk):
        record = get_object_or_404(
            InspectionRecord, pk=record_pk, work_order__case=case
        )
        items = list(record.items.select_related("point"))
        form = InspectionResultsForm(request.POST, items=items)
        if not form.is_valid():
            raise FormInvalid("inspection_form", form)
        changed = record_inspection_results(record, request.user, form.results())
        if not changed:
            return _("No inspection point was changed.")
        return _("%(n)s inspection point(s) recorded.") % {"n": changed}


class InspectionSignView(CaseActionView):
    """Sign the completed full inspection (SVC-08)."""

    def perform(self, request, case, record_pk):
        record = get_object_or_404(
            InspectionRecord, pk=record_pk, work_order__case=case
        )
        sign_inspection_record(record, request.user)
        return _("Full inspection #%(id)s signed.") % {"id": record.pk}

class WorkOrderFinalizeView(CaseActionView):
    """Mark the work order as final (SVC-05)."""

    def perform(self, request, case):
        work_order = getattr(case, "work_order", None)
        if work_order is None:
            raise ValidationError(
                _("Case #%(id)s has no work order yet.") % {"id": case.pk}
            )
        finalize_work_order(work_order, request.user)
        return _("Work order #%(id)s marked final.") % {"id": work_order.pk}


class WorkOrderApprovalUploadView(CaseActionView):
    """Upload the client-signed work-order scan - the client's legal
    approval (SVC-05)."""

    def perform(self, request, case):
        work_order = getattr(case, "work_order", None)
        if work_order is None:
            raise ValidationError(
                _("Case #%(id)s has no work order yet.") % {"id": case.pk}
            )
        form = WorkOrderApprovalUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("work_order_approval_form", form)
        attach_client_approval(work_order, form.cleaned_data["file"], request.user)
        return _("Client approval uploaded onto work order #%(id)s.") % {
            "id": work_order.pk
        }


class WorkOrderSignView(CaseActionView):
    """Technician signature on the work order - the company-side record
    of when the work item actually started (SVC-05)."""

    def perform(self, request, case):
        work_order = getattr(case, "work_order", None)
        if work_order is None:
            raise ValidationError(
                _("Case #%(id)s has no work order yet.") % {"id": case.pk}
            )
        sign_work_order(work_order, request.user)
        return _("Work order #%(id)s signed.") % {"id": work_order.pk}


def _work_order_or_error(case):
    work_order = getattr(case, "work_order", None)
    if work_order is None:
        raise ValidationError(
            _("Case #%(id)s has no work order yet.") % {"id": case.pk}
        )
    return work_order


class WorkLogAddView(CaseActionView):
    """Record one performed-work entry: work done + time spent (SVC-15)."""

    def perform(self, request, case):
        work_order = _work_order_or_error(case)
        form = WorkLogEntryForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("work_log_form", form)
        entry = log_work(
            work_order,
            request.user,
            description=form.cleaned_data["description"],
            hours=form.cleaned_data["hours"],
        )
        return _("Work log entry #%(id)s recorded (%(hours)s h).") % {
            "id": entry.pk,
            "hours": entry.hours,
        }


class UsedPartAddView(CaseActionView):
    """Record an installed part on the work order (SVC-15)."""

    def perform(self, request, case):
        work_order = _work_order_or_error(case)
        form = UsedPartForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("used_part_form", form)
        used = record_used_part(
            work_order,
            request.user,
            part=form.cleaned_data["part"],
            quantity=form.cleaned_data["quantity"],
        )
        return _("Part '%(part)s' x%(qty)s recorded as installed.") % {
            "part": used.part,
            "qty": used.quantity,
        }


class WorkDocsUploadView(CaseActionView):
    """Optional photos/documents of the executed work (SVC-15); stored
    as WORK-stage attachments on the case, kind derived per file."""

    def perform(self, request, case):
        from apps.attachments.models import VIDEO_EXTENSIONS, Attachment

        from .forms import PHOTO_EXTENSIONS

        form = WorkDocsUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("work_docs_form", form)
        count = 0
        for upload in form.cleaned_data["files"]:
            extension = upload.name.rsplit(".", 1)[-1].lower()
            if extension in PHOTO_EXTENSIONS:
                kind = Attachment.Kind.PHOTO
            elif extension in VIDEO_EXTENSIONS:
                kind = Attachment.Kind.VIDEO
            else:
                kind = Attachment.Kind.DOCUMENT
            attach_file(
                case, upload, kind, Attachment.Stage.WORK, user=request.user
            )
            count += 1
        return _("%(count)s work file(s) uploaded.") % {"count": count}


class TestRunRecordView(CaseActionView):
    """Quality-control record: comment + PASS/FAIL approval (SVC-16).
    A FAIL in ``quality_check`` sends the case back to rework."""

    def perform(self, request, case):
        work_order = _work_order_or_error(case)
        form = TestRunForm(request.POST, case=case)
        if not form.is_valid():
            raise FormInvalid("test_run_form", form)
        test_run = record_test_run(
            work_order,
            form.cleaned_data["result"],
            request.user,
            notes=form.cleaned_data["notes"],
            kind=form.cleaned_data["kind"],
            fault_cleared=form.cleaned_data["fault_cleared"],
        )
        if test_run.result == test_run.Result.PASSED:
            return _("Test run #%(id)s recorded: PASSED.") % {"id": test_run.pk}
        return _(
            "Test run #%(id)s recorded: FAILED - the case goes back to rework."
        ) % {"id": test_run.pk}


class CompletionNoticeSendView(CaseActionView):
    """Compose and send/draft the client completion notice (SVC-17)."""

    def perform(self, request, case):
        form = CompletionNoticeForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("completion_notice_form", form)
        notice = send_completion_notice(
            case,
            request.user,
            works_performed=form.cleaned_data["works_performed"],
            final_cost=form.cleaned_data["final_cost"],
            handover_mode=form.cleaned_data["handover_mode"],
            scheduled_for=form.cleaned_data["scheduled_for"],
            channel=form.cleaned_data["channel"],
            cost_note=form.cleaned_data["cost_note"],
        )
        if notice.channel == notice.Channel.EMAIL:
            if notice.sent_at:
                return _("Completion notice #%(id)s e-mailed to the client.") % {
                    "id": notice.pk
                }
            return _(
                "Completion notice #%(id)s composed, but the e-mail could "
                "not be sent - check the e-mail log."
            ) % {"id": notice.pk}
        return _(
            "Completion notice #%(id)s drafted as SMS - copy the text below "
            "and send it from the phone."
        ) % {"id": notice.pk}


class HandoverTechnicianSignView(CaseActionView):
    """The responsible worker digitally signs the handover (SVC-18)."""

    def perform(self, request, case, handover_pk):
        handover = get_object_or_404(HandoverRecord, pk=handover_pk, case=case)
        sign_handover_technician(handover, request.user)
        return _("Handover record #%(id)s signed by the worker.") % {
            "id": handover.pk
        }


class HandoverMediaUploadView(CaseActionView):
    """§7/SVC-07: photograph the asset at handover, the mirror of the
    arrival photos the intake form already demands. They are stored as
    HANDOVER-stage attachments on the case (INV-08) - which is where
    ``compare_handover_with_intake()`` counts them, so the count the
    comparison card has always displayed finally has something to
    count."""

    def perform(self, request, case, handover_pk):
        from django.db import transaction

        from apps.attachments.models import Attachment

        handover = get_object_or_404(HandoverRecord, pk=handover_pk, case=case)
        form = HandoverMediaUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("handover_media_form", form)
        data = form.cleaned_data
        # Filed on the RECORD, not the case: a case may hold several
        # handovers, and each has to be photographed for itself.
        with transaction.atomic():
            for upload in data["photos"]:
                attach_file(
                    handover,
                    upload,
                    Attachment.Kind.PHOTO,
                    Attachment.Stage.HANDOVER,
                    user=request.user,
                )
            for upload in data["videos"]:
                attach_file(
                    handover,
                    upload,
                    Attachment.Kind.VIDEO,
                    Attachment.Stage.HANDOVER,
                    user=request.user,
                )
        return _(
            "Handover documentation uploaded (%(photos)s photo(s), "
            "%(videos)s video(s))."
        ) % {"photos": len(data["photos"]), "videos": len(data["videos"])}


class HandoverApprovalUploadView(CaseActionView):
    """Upload the client-signed handover approval scan (SVC-18)."""

    def perform(self, request, case, handover_pk):
        handover = get_object_or_404(HandoverRecord, pk=handover_pk, case=case)
        form = HandoverApprovalUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("handover_approval_form", form)
        attach_handover_approval(handover, form.cleaned_data["file"], request.user)
        return _("Client handover approval uploaded.")


class HandoverExportView(StaffRequiredMixin, View):
    """Printable / Word export of the intake-vs-handover difference
    (SVC-18). ``?format=doc`` downloads an HTML-based .doc that opens in
    Word; without it the page renders print-ready in the browser."""

    def get(self, request, pk, handover_pk):
        case = get_object_or_404(Case, pk=pk)
        handover = get_object_or_404(HandoverRecord, pk=handover_pk, case=case)
        context = {
            "case": case,
            "handover": handover,
            "comparison": compare_handover_with_intake(handover),
            "as_document": request.GET.get("format") == "doc",
        }
        response = render(request, "backoffice/handover_export.html", context)
        if context["as_document"]:
            response["Content-Type"] = "application/msword"
            response["Content-Disposition"] = (
                'attachment; filename="handover-record-%s.doc"' % case.pk
            )
        return response


class IntakeCreateView(CaseActionView):
    """Create the intake record with its arrival documentation
    (SVC-06/SVC-07): mandatory visible-damage description and arrival
    photos, optional videos - files are stored as INTAKE-stage
    attachments on the case (INV-08)."""

    def perform(self, request, case):
        from django.db import transaction

        from apps.attachments.models import Attachment

        form = IntakeRecordCreateForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("intake_form", form)
        data = form.cleaned_data
        with transaction.atomic():
            record = create_intake_record(
                case,
                request.user,
                mount_status=data["mount_status"],
                cleanliness_status=data["cleanliness_status"],
                storage_location=data["storage_location"],
                energy_level=data["energy_level"],
                damage_note=data["damage_note"],
                general_condition_note=data["general_condition_note"],
            )
            for upload in data["photos"]:
                attach_file(
                    case,
                    upload,
                    Attachment.Kind.PHOTO,
                    Attachment.Stage.INTAKE,
                    user=request.user,
                )
            for upload in data["videos"]:
                attach_file(
                    case,
                    upload,
                    Attachment.Kind.VIDEO,
                    Attachment.Stage.INTAKE,
                    user=request.user,
                )
            # WNT rule 4: the pickup card shows a "Receipt"
            # badge off SitePickup.intake_record, but nothing ever wrote
            # that FK - so the badge stayed amber even once the receipt for
            # this very case existed. Only link an existing pickup; never
            # fabricate one, and never overwrite a link already on file.
            if case.case_type == CaseType.STORAGE:
                agreement = getattr(case, "storage_agreement", None)
                pickup = getattr(agreement, "site_pickup", None)
                if pickup is not None and pickup.intake_record_id is None:
                    upsert_site_pickup(
                        agreement, user=request.user, intake_record=record
                    )
        return _(
            "Intake record #%(id)s created (%(photos)s photo(s), "
            "%(videos)s video(s))."
        ) % {
            "id": record.pk,
            "photos": len(data["photos"]),
            "videos": len(data["videos"]),
        }


class IntakeAmendmentAddView(CaseActionView):
    """SVC-07 / INV-12: append a later-discovered condition to a signed
    intake record. The service existed from the start; until now nothing
    in the application called it, so this was admin-only."""

    def perform(self, request, case, record_pk):
        record = get_object_or_404(IntakeRecord, pk=record_pk, case=case)
        form = IntakeAmendmentForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("intake_amendment_form", form)
        amendment = append_intake_amendment(
            record, form.cleaned_data["text"], request.user
        )
        return _(
            "Amendment #%(id)s appended to intake record #%(record)s."
        ) % {"id": amendment.pk, "record": record.pk}


class IntakeNoItemsView(CaseActionView):
    """SVC-06: state that nothing was received with the asset, so an
    empty inventory is an answer instead of an omission."""

    def perform(self, request, case, record_pk):
        record = get_object_or_404(IntakeRecord, pk=record_pk, case=case)
        confirm_no_intake_items(record, request.user)
        return _(
            "Recorded: nothing was received with the asset "
            "(intake record #%(id)s)."
        ) % {"id": record.pk}


class IntakeSignView(CaseActionView):
    """Staff receipt confirmation on the intake record (SVC-06) -
    attributed to the responsible technician when one is assigned."""

    def perform(self, request, case, record_pk):
        record = get_object_or_404(IntakeRecord, pk=record_pk, case=case)
        sign_intake_record(record, request.user)
        return _("Intake record #%(id)s signed.") % {"id": record.pk}


class IntakeItemAddView(CaseActionView):
    """List an inventory item on an unsigned intake record (SVC-06)."""

    def perform(self, request, case, record_pk):
        record = get_object_or_404(IntakeRecord, pk=record_pk, case=case)
        form = IntakeItemForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("intake_item_form", form)
        item = add_intake_item(record, request.user, **form.cleaned_data)
        return _("Item '%(name)s' listed on intake record #%(id)s.") % {
            "name": item.item_name,
            "id": record.pk,
        }


class IntakeItemDeleteView(CaseActionView):
    """Remove a mistakenly listed item while the record is unsigned."""

    def perform(self, request, case, record_pk, item_pk):
        item = get_object_or_404(
            IntakeItem, pk=item_pk, record__pk=record_pk, record__case=case
        )
        remove_intake_item(item, request.user)
        return _("Item removed from intake record #%(id)s.") % {"id": record_pk}


class TransportDeliveredView(CaseActionView):
    """Record the physical delivery of the asset (TRP-08)."""

    def perform(self, request, case):
        order = getattr(case, "transport_order", None)
        if order is None:
            raise ValidationError(
                _("Case #%(id)s has no transport order.") % {"id": case.pk}
            )
        mark_delivered(order, request.user, note=request.POST.get("note", ""))
        return _("Transport order #%(id)s marked delivered.") % {"id": order.pk}


class TransportLoadingView(CaseActionView):
    """Record the internal loading confirmation (TRP-07).

    Truck/trailer and securing/road-readiness, whether a crane was used,
    the carrier present and whether the client was told outside the app.
    Submitting signs off as the responsible person (``signed_by`` /
    ``signed_at`` set in the service).
    """

    def perform(self, request, case):
        form = LoadingConfirmationForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("loading_confirmation_form", form)
        record_loading_confirmation(
            case, user=request.user, **form.cleaned_data
        )
        return _("Loading confirmed for case #%(id)s (TRP-07).") % {"id": case.pk}


class TransportLoadingPlanView(CaseActionView):
    """Record the agreed loading/unloading plan (TRP-02).

    Where the asset is set down, whether a crane is available, what the
    site is like, who will be there to receive it and the agreed
    loading and unloading times - all settled with the client before the
    asset moves. ``_guard_in_transit`` refuses a departure until the
    substantive items are answered.
    """

    def perform(self, request, case):
        form = LoadingPlanForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("loading_plan_form", form)
        record_loading_plan(case, user=request.user, **form.cleaned_data)
        return _("Loading plan saved for case #%(id)s (TRP-02).") % {
            "id": case.pk
        }


class TransportDeliveryConfirmView(CaseActionView):
    """Record the delivery protocol (TRP-08).

    Unloading organized and asset put into service, the attending
    owner or authorized representative (a named person is mandatory), the
    accompanying company employee and notes. Submitting signs off as the
    staff member (``signed_by`` / ``signed_at`` set in the service) and
    stamps ``delivered_at`` on the order when not set yet.
    """

    def perform(self, request, case):
        form = DeliveryConfirmationForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("delivery_confirmation_form", form)
        record_delivery_confirmation(
            case, user=request.user, **form.cleaned_data
        )
        return _("Delivery confirmed for case #%(id)s (TRP-08).") % {"id": case.pk}


class TransportDeliveryProtocolUploadView(CaseActionView):
    """Upload the signed delivery-protocol scan onto the confirmation
    (TRP-08)."""

    def perform(self, request, case):
        order = getattr(case, "transport_order", None)
        confirmation = (
            getattr(order, "delivery_confirmation", None)
            if order is not None
            else None
        )
        if confirmation is None:
            raise ValidationError(
                _(
                    "Case #%(id)s has no delivery confirmation yet - record "
                    "the delivery protocol first (TRP-08)."
                ) % {"id": case.pk}
            )
        form = DeliveryProtocolUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("delivery_protocol_form", form)
        attachment = attach_delivery_protocol(
            confirmation, form.cleaned_data["file"], request.user
        )
        return _("Signed delivery protocol '%(name)s' uploaded (TRP-08).") % {
            "name": attachment.original_name
        }


class TransportCarrierPriceView(CaseActionView):
    """Declare the found external-carrier price for a large asset (TRP-03).

    For an asset own transport cannot handle (over ~10 m) no standard price
    applies, so a separate carrier offer is obtained; recording it here
    notifies staff that the transport can now be arranged.
    """

    def perform(self, request, case):
        order = getattr(case, "transport_order", None)
        if order is None:
            raise ValidationError(
                _("Case #%(id)s has no transport order.") % {"id": case.pk}
            )
        form = TransportCarrierPriceForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("carrier_price_form", form)
        declare_carrier_price(
            order,
            form.cleaned_data["carrier_price"],
            request.user,
            carrier_name=form.cleaned_data["carrier_name"],
        )
        return _("Carrier price recorded for case #%(id)s (TRP-03).") % {
            "id": case.pk
        }


class AttachmentUploadView(CaseActionView):
    """Upload a file onto the case (INV-08).

    The one action that outlives the case: paperwork arrives late - a
    signed scan posted back, a photo someone had on their phone - and
    filing it against the case it belongs to beats filing it nowhere.
    It is purely additive and it is logged; it cannot change any guard's
    answer, because a closed case has no transitions left to guard.
    """

    allowed_when_closed = True

    def perform(self, request, case):
        form = AttachmentUploadForm(request.POST, request.FILES)
        if not form.is_valid():
            raise FormInvalid("attachment_form", form)
        attachment = attach_file(
            case,
            form.cleaned_data["file"],
            form.cleaned_data["kind"],
            form.cleaned_data["stage"],
            user=request.user,
        )
        return _("Attachment #%(id)s uploaded.") % {"id": attachment.pk}


class QuoteCreateView(CaseActionView):
    """Draft a quote on the case (SVC-11/SVC-12)."""

    def perform(self, request, case):
        form = QuoteCreateForm(
            request.POST, with_catalog=case.case_type == CaseType.SERVICE
        )
        if not form.is_valid():
            raise FormInvalid("quote_form", form)
        quote = create_quote(
            case,
            form.cleaned_data["description"],
            form.cleaned_data["amount"],
            user=request.user,
            valid_until=form.cleaned_data["valid_until"],
        )
        items = form.cleaned_data.get("catalog_items")
        if items:
            # ACT-02 audit line; the form owns the one definition of the
            # suggested sum (hours included), the service writes the log.
            log_price_list_basis(
                quote, request.user, items, form.suggested_total(items)
            )
        return _("Quote #%(id)s created (%(amount)s %(currency)s).") % {
            "id": quote.pk,
            "amount": quote.amount,
            "currency": quote.currency,
        }


class StorageOfferCreateView(CaseActionView):
    """WNT-03: draft the offer quote for a standalone storage service or
    a package arrangement; renewal vs. recommended-option handling and
    the commercial-period text live in ``create_storage_offer``."""

    def perform(self, request, case):
        form = StorageOfferForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("storage_offer_form", form)
        quote = create_storage_offer(
            case,
            offer_type=form.cleaned_data["offer_type"],
            description=form.cleaned_data["description"],
            amount=form.cleaned_data["amount"],
            package=form.cleaned_data["package"],
            period_start=form.cleaned_data["period_start"],
            period_end=form.cleaned_data["period_end"],
            valid_until=form.cleaned_data["valid_until"],
            user=request.user,
        )
        return _("Offer quote #%(id)s created (%(amount)s %(currency)s).") % {
            "id": quote.pk,
            "amount": quote.amount,
            "currency": quote.currency,
        }


class AdvanceMarkPaidView(CaseActionView):
    """Record the actual payment of an advance (TRP-04/INV-05)."""

    def perform(self, request, case, advance_pk):
        advance = get_object_or_404(AdvancePayment, pk=advance_pk, case=case)
        mark_advance_paid(advance, request.user)
        return _("Advance payment #%(id)s marked paid.") % {"id": advance.pk}


class TransportAdvanceDeriveView(CaseActionView):
    """TRP-04: (re-)derive the mandatory 50% advance from the transport
    offer.

    ``QuoteSendView`` already does this when the offer is sent, so this is
    the repair path: cases whose offer predates the derivation, and offers
    revised afterwards. Never touches a paid advance.
    """

    def perform(self, request, case):
        if case.case_type != CaseType.TRANSPORT:
            raise ValidationError(
                _("Case #%(id)s is not a transport case.") % {"id": case.pk}
            )
        advance = ensure_transport_advance(case, user=request.user)
        if advance is None:
            raise ValidationError(
                _(
                    "No sent offer to derive the advance from - create and "
                    "send the transport offer first (TRP-01)."
                )
            )
        return _(
            "Advance payment #%(id)s is %(percent)s%% of offer #%(quote)s: "
            "%(amount)s €."
        ) % {
            "id": advance.pk,
            "percent": advance.percent,
            "quote": advance.quote_id or "-",
            "amount": advance.amount,
        }


class StorageDocsUpdateView(CaseActionView):
    """WNT-01: create/update the inquiry documentation on the storage
    agreement (permit, dimensions, agreed price) from the case page."""

    def perform(self, request, case):
        agreement = getattr(case, "storage_agreement", None)
        form = StorageAgreementForm(request.POST, instance=agreement)
        if not form.is_valid():
            raise FormInvalid("storage_docs_form", form)
        agreement = record_storage_entry(
            case, user=request.user, **form.cleaned_data
        )
        # OPEN-05: a deviating agreed price is allowed but surfaced.
        note = record_price_deviation(agreement, user=request.user)
        if note is not None:
            messages.info(request, note)
        return _("Storage documentation saved on agreement #%(id)s.") % {
            "id": agreement.pk
        }


class StorageSitePickupView(CaseActionView):
    """WNT rule 4 "Pickup from site": record the site pickup
    logistics (organizer, lift-out, delivery, status) on the storage
    agreement from the case page."""

    def perform(self, request, case):
        agreement = getattr(case, "storage_agreement", None)
        pickup = getattr(agreement, "site_pickup", None) if agreement else None
        form = SitePickupForm(request.POST, instance=pickup)
        if not form.is_valid():
            raise FormInvalid("site_pickup_form", form)
        pickup = record_site_pickup(case, user=request.user, **form.cleaned_data)
        return _("Site pickup saved for case #%(id)s.") % {"id": case.pk}


class StoragePositionAssignView(CaseActionView):
    """WNT-06: assign a hall/row/slot placement; exit-order conflicts
    from ``assign_position`` surface as error messages."""

    def perform(self, request, case):
        agreement = getattr(case, "storage_agreement", None)
        if agreement is None:
            raise ValidationError(
                _(
                    "Record the storage documentation first - the position "
                    "is assigned on the agreement."
                )
            )
        form = StoragePositionAssignForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("storage_position_form", form)
        position = assign_position(
            agreement,
            form.cleaned_data["hall"],
            form.cleaned_data["row"],
            form.cleaned_data["slot"],
            form.cleaned_data["planned_exit_date"],
        )
        return _("Storage position %(pos)s assigned.") % {"pos": position}


# WNT-05: exit-order colours for the yard assets, earliest leaver first.
#
# The ramp used to run green -> yellow -> red, which failed twice over.
# Red/green is the one pair the ~8% of men with deuteranopia cannot
# separate, and it is exactly the pair the *ghost* needs for "this
# placement is legal / illegal" - so the same two colours were carrying
# two unrelated meanings on one screen. This ramp is ordered by lightness
# as well as hue (a viridis-style dark-violet -> teal -> yellow), which
# survives both colour blindness and a monochrome printout, and it leaves
# green/red free to mean legal/illegal and nothing else.
#
# Each stop carries its own ink, because a label drawn in one fixed dark
# colour is unreadable on the dark end of any ordered ramp.
_EXIT_PALETTE = (
    ("#440154", "#ffffff"),
    ("#414487", "#ffffff"),
    ("#2a788e", "#ffffff"),
    ("#22a884", "#06231c"),
    ("#7ad151", "#10202a"),
    ("#fde725", "#10202a"),
)


def _exit_sort_key(planned_exit_date):
    # No planned exit date == "leaves last".
    return planned_exit_date or datetime.date.max


def _exit_colours(positions) -> dict:
    """Map each distinct planned-exit key in ``positions`` to a
    ``(fill, ink)`` pair from the ramp, earliest exit first."""
    keys = sorted({_exit_sort_key(p.planned_exit_date) for p in positions})
    span = max(len(keys) - 1, 1)
    last = len(_EXIT_PALETTE) - 1
    return {
        key: _EXIT_PALETTE[round(i / span * last) if len(keys) > 1 else 0]
        for i, key in enumerate(keys)
    }


def _outline_label_size(w, h) -> float:
    """Type size (in metres, the SVG's unit) for the label inside a outline.

    A fixed size wrote "#12 - 10.5m" about six metres wide across an asset
    with a four-metre breadth. Scaling to the narrow dimension keeps the
    label inside the shape it names; the clamp stops a 30 m asset getting
    billboard lettering and a small item getting nothing legible.
    """
    return max(0.30, min(0.85, float(min(w, h)) / 4.0))


def build_zone_views(zones, place_case=None) -> list:
    """WNT-05 shared yard rendering: each active zone with its
    coordinate-placed assets drawn to scale (metre coordinates) and
    coloured by planned exit. Used by the yard plan and the pre-contract
    fit-check, so both draw occupied ground identically.

    Each zone view also carries the zone's obstacles (ground no asset may
    occupy) and an occupancy summary, so the page can answer "how full is
    this yard?" without a second query.

    ``place_case`` (optional) flags the asset currently being positioned
    so the template can highlight it.
    """
    zone_views = []
    for zone in zones:
        positions = [p for p in zone.positions.all() if p.is_coordinate_placed]
        colours = _exit_colours(positions)
        assets = []
        used_area = Decimal("0")
        for position in positions:
            try:
                length, width = asset_footprint_size(position.agreement)
            except ValidationError:
                continue
            x0, y0, x1, y1 = zone_footprint(
                length,
                width,
                position.pos_x_m,
                position.pos_y_m,
                position.orientation,
            )
            case = position.agreement.case
            fill, ink = colours[_exit_sort_key(position.planned_exit_date)]
            w, h = x1 - x0, y1 - y0
            used_area += w * h
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            size = _outline_label_size(w, h)
            assets.append(
                {
                    "x": x0,
                    "y": y0,
                    "w": w,
                    "h": h,
                    # WNT-09: the outline drawn inside the footprint, bow
                    # towards the exit (or the left edge when broadside).
                    "d": outline_path(x0, y0, x1, y1, position.orientation),
                    "crosswise": position.orientation == StorageOrientation.CROSSWISE,
                    "cx": cx,
                    "cy": cy,
                    "fill": fill,
                    "ink": ink,
                    "label_size": size,
                    # Both label lines are positioned in user units. An
                    # "em" offset is a legal SVG length but not a bare
                    # number, and this geometry is machine-parsed.
                    "label_y1": float(cy) - size * 0.55,
                    "label_y2": float(cy) + size * 0.65,
                    "case": case,
                    "length": length,
                    "exit_date": position.planned_exit_date,
                    # Printed inside the outline so the exit order can be read
                    # without relying on colour at all (WNT-05).
                    "exit_label": (
                        formats.date_format(position.planned_exit_date, "M")
                        if position.planned_exit_date
                        else "-"
                    ),
                    "is_current": place_case is not None
                    and case.pk == place_case.pk,
                }
            )
        obstacles = [
            {
                "x": o.pos_x_m,
                "y": o.pos_y_m,
                "w": o.width_m,
                "h": o.length_m,
                "cx": o.pos_x_m + o.width_m / 2,
                "cy": o.pos_y_m + o.length_m / 2,
                "name": o.name,
                "kind": o.get_kind_display(),
                "label_size": _outline_label_size(o.width_m, o.length_m),
            }
            for o in zone.obstacles.all()
        ]
        blocked_area = sum(
            (o["w"] * o["h"] for o in obstacles), Decimal("0")
        )
        area = zone.area_m2 or Decimal("0")
        zone_views.append(
            {
                "zone": zone,
                "assets": assets,
                "obstacles": obstacles,
                # WNT-09: the site plan drawn under the grid, if uploaded.
                "plan_image_url": zone.plan_image.url if zone.plan_image else "",
                "asset_count": len(assets),
                "used_pct": (
                    int(round(float(used_area + blocked_area) / float(area) * 100))
                    if area
                    else 0
                ),
            }
        )
    return zone_views


def zone_payload(zone_views) -> list:
    """The yard as plain JSON for the client-side placement preview.

    The browser holds every fact ``validate_free_placement()`` uses - the
    other outlines' rectangles and exit dates, the reserved ground, the zone
    extent and its clearance - so the ghost can refuse a spot at the
    moment of aiming instead of after a round trip. The server still
    re-validates every POST; this payload is a preview, never the gate.

    Decimals are handed over as strings by ``DjangoJSONEncoder`` and
    ``parseFloat``-ed on the other side, which is also what keeps the
    Serbian decimal comma out of machine-read numbers.
    """
    return [
        {
            "pk": zv["zone"].pk,
            "name": zv["zone"].name,
            "width": zv["zone"].width_m,
            "length": zv["zone"].length_m,
            "clearance": zv["zone"].clearance_m,
            "assets": [
                {
                    "x": b["x"],
                    "y": b["y"],
                    "w": b["w"],
                    "h": b["h"],
                    "d": b["d"],
                    "case": b["case"].pk,
                    # ISO sorts lexicographically, and a missing exit date
                    # is "leaves last" - the same rule _exit_sort_key uses.
                    "exit": (
                        b["exit_date"].isoformat() if b["exit_date"] else "9999-12-31"
                    ),
                }
                for b in zv["assets"]
            ],
            "obstacles": [
                {
                    "x": o["x"],
                    "y": o["y"],
                    "w": o["w"],
                    "h": o["h"],
                    "name": o["name"],
                }
                for o in zv["obstacles"]
            ],
        }
        for zv in zone_views
    ]


def yard_labels() -> dict:
    """The verdicts the client-side placer prints.

    They live here rather than in the template so they are extracted for
    translation like every other string, and so both yard pages say the
    same thing about the same rule. ``{case}``, ``{name}`` and ``{gap}``
    are filled in by ``yard.js``; each mirrors the wording of the
    ``ValidationError`` the server would raise for the same spot.
    """
    return {
        "fits": _("Fits here"),
        "tooBig": _("Too large for this yard"),
        "outside": _("Runs past the yard edge"),
        "onObstacle": _("Reserved ground: {name}"),
        "collides": _("Overlaps asset #{case}"),
        "tooClose": _("Too close to #{case} - this yard keeps {gap} m free"),
        "trappedBy": _("Blocked in by #{case}, which leaves later"),
        "wouldTrap": _("Would block #{case}, which leaves earlier"),
    }


class StorageLayoutView(StaffRequiredMixin, TemplateView):
    """WNT-05: the graphic yard plan ("Raspored u storage"). Draws every
    active zone to scale with the assets placed in it and the ground
    reserved by obstacles; ``?case=<pk>`` puts the page in placing mode
    for that case's asset (aim, confirm, place).

    The page also answers the question it could not take before: *what is
    still waiting for a spot?* - so the yard can be worked from the yard
    instead of one case at a time.
    """

    template_name = "backoffice/storage_layout.html"

    #: Set by ``StoragePlaceView`` when it re-renders after a refusal, so
    #: the planner gets their own numbers back instead of an empty form.
    bound_form = None
    #: Overrides ``?case=`` when the page is rendered from a POST.
    place_case_id = None

    def get_place_case_id(self):
        if self.place_case_id is not None:
            return self.place_case_id
        raw = self.request.GET.get("case")
        # ?case=abc went straight into get_object_or_404(pk=...), which
        # raises ValueError rather than Http404 - a 500 on a malformed URL
        # anyone can produce by editing the address bar. A non-numeric case
        # is simply not a case, so the page renders without placing mode.
        return raw if raw and raw.isdigit() else None

    def get_zones(self):
        return list(
            StorageZone.objects.filter(is_active=True).prefetch_related(
                "positions__agreement__case__client", "obstacles"
            )
        )

    def get_waiting(self):
        """Contracted assets with no spot on the plan yet.

        An asset is "waiting" when its agreement has no coordinate
        placement and its case is still open. Assets whose length was
        never recorded are listed too, flagged - they are precisely the
        ones that cannot be placed, and hiding them is how they get
        forgotten until October.
        """
        agreements = (
            StorageAgreement.objects.filter(
                case__case_type=CaseType.STORAGE
            )
            .exclude(positions__zone__isnull=False)
            .select_related("case", "case__status", "case__client")
            .order_by("case__pk")
        )
        waiting = []
        for agreement in agreements:
            case = agreement.case
            if case.status_id and case.status.is_terminal:
                continue
            try:
                length, width = asset_footprint_size(agreement)
            except ValidationError:
                length = width = None
            waiting.append(
                {
                    "case": case,
                    "client": case.client,
                    "length": length,
                    "width": width,
                    "placeable": length is not None,
                }
            )
        return waiting

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        zones = self.get_zones()

        # Placing mode: the asset to position comes from ?case=.
        place_case = None
        place_agreement = None
        asset_dims = None
        case_id = self.get_place_case_id()
        if case_id:
            place_case = get_object_or_404(
                Case, pk=case_id, case_type=CaseType.STORAGE
            )
            place_agreement = getattr(place_case, "storage_agreement", None)
            if place_agreement is not None:
                try:
                    length, width = asset_footprint_size(place_agreement)
                    asset_dims = {"length": length, "width": width}
                except ValidationError as exc:
                    messages.warning(self.request, "; ".join(exc.messages))
        context["place_case"] = place_case
        context["place_agreement"] = place_agreement
        context["asset_dims"] = asset_dims
        context["placed_position"] = (
            place_agreement.positions.filter(zone__isnull=False).first()
            if place_agreement is not None
            else None
        )

        # ?placed= highlights the asset that was just positioned WITHOUT
        # putting the page back into placing mode. Returning to ?case=
        # after a successful placement left the placer armed for the same
        # asset, so the next sweep of the pointer drew an outline that read
        # as a second, unidentified asset appearing on the plan.
        highlight = place_case
        if highlight is None:
            placed_id = self.request.GET.get("placed")
            if placed_id and placed_id.isdigit():
                highlight = Case.objects.filter(
                    pk=placed_id, case_type=CaseType.STORAGE
                ).first()
        zone_views = build_zone_views(zones, place_case=highlight)
        context["zone_views"] = zone_views
        context["yard_data"] = zone_payload(zone_views)
        context["yard_labels"] = yard_labels()
        context["waiting"] = self.get_waiting()
        context["can_edit_yard"] = _may_edit_pricing(self.request.user)

        if place_case is None:
            context["placement_form"] = None
        elif self.bound_form is not None:
            # Stage 1 of the yard plan: a refused placement used to
            # redirect to a freshly built form, so the planner re-picked
            # the zone, re-set the orientation and retyped the exit date
            # before they could even try again. Handing back the bound
            # form makes the second attempt cost one correction.
            context["placement_form"] = self.bound_form
        else:
            context["placement_form"] = StoragePlacementForm(
                initial={"zone": zones[0].pk if zones else None}
            )
        return context


class StoragePlaceView(StorageLayoutView):
    """WNT-05 POST target for click-to-place: position the case's asset on
    the yard via ``place_asset``; bounds, obstacle, collision, clearance
    and exit-order errors from the service surface as messages.

    A refusal re-renders the yard *in place* with the submitted values
    still in the form, rather than redirecting to an empty one. Only
    success redirects, so a reload cannot re-post the placement.
    """

    http_method_names = ["post"]

    def _refused(self, form):
        self.bound_form = form
        return self.render_to_response(self.get_context_data())

    def post(self, request, pk):
        case = get_object_or_404(
            Case, pk=pk, case_type=CaseType.STORAGE
        )
        self.place_case_id = str(case.pk)
        # INV-15 and a status gate: the yard plan is not a CaseActionView,
        # so a closed case could still be moved about on it, and a bare
        # inquiry could take ground (QA S-H2, S-H3).
        refusal = closed_case_message(case)
        if refusal is None and not has_reached_status(case, "contracted"):
            refusal = _(
                "Case #%(id)s is not contracted yet - ground on the yard is "
                "reserved for contracted storage (WNT-05)."
            ) % {"id": case.pk}
        if refusal is not None:
            messages.error(request, refusal)
            return redirect(reverse("backoffice:storage-layout"))
        agreement = getattr(case, "storage_agreement", None)
        if agreement is None:
            messages.error(
                request,
                _(
                    "Record the storage documentation first - the asset is "
                    "placed on its agreement."
                ),
            )
            return self._refused(None)
        form = StoragePlacementForm(request.POST)
        if not form.is_valid():
            messages.error(
                request, _("Check the highlighted fields and place again.")
            )
            return self._refused(form)
        try:
            position = place_asset(
                agreement,
                form.cleaned_data["zone"],
                form.cleaned_data["pos_x_m"],
                form.cleaned_data["pos_y_m"],
                form.cleaned_data["orientation"],
                form.cleaned_data["planned_exit_date"],
            )
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return self._refused(form)
        messages.success(
            request, _("Asset placed on the yard: %(pos)s.") % {"pos": position}
        )
        return redirect(
            "%s?placed=%s" % (reverse("backoffice:storage-layout"), case.pk)
        )


class StorageUnplaceView(StaffRequiredMixin, View):
    """WNT-05 "lift out": take the case's asset back off the yard plan.

    A placement was a one-way door - an asset put in the wrong zone could
    only be moved somewhere else, never removed, so a cancelled contract
    kept occupying ground nobody could reclaim. Removing the position
    frees the ground and puts the asset back on the waiting list; the
    agreement itself is untouched.
    """

    def post(self, request, pk):
        case = get_object_or_404(
            Case, pk=pk, case_type=CaseType.STORAGE
        )
        refusal = closed_case_message(case)
        if refusal is not None:
            messages.error(request, refusal)
            return redirect(reverse("backoffice:storage-layout"))
        agreement = getattr(case, "storage_agreement", None)
        if agreement is not None and unplace_asset(agreement):
            messages.success(
                request,
                _("Asset #%(id)s lifted out - its ground is free again.")
                % {"id": case.pk},
            )
        else:
            messages.error(request, _("That asset is not placed on the yard."))
        # Back to plain viewing: the asset is on the waiting rail now, and
        # arming the placer for it would put an outline on the plan nobody
        # asked for.
        return redirect(reverse("backoffice:storage-layout"))


class StorageFitCheckView(StaffRequiredMixin, TemplateView):
    """WNT-05 pre-contract fit-check ("Provjera mjesta u storage").

    Draws the yard with every occupied spot and lets staff slide an asset
    of arbitrary dimensions around, *before anything is contracted*, to see
    whether it fits an empty spot and still leaves room for the storage
    services. Pure what-if: no case is required, the bounds/collision check
    runs client-side against the drawn assets, and nothing is written. To
    commit a placement, staff use the yard plan (``storage-layout``) on the
    case once the agreement exists.

    ``?length=`` and ``?width=`` (metres) pre-fill the trial footprint, so
    a case page can link here with the inquiry's asset dimensions.
    """

    template_name = "backoffice/storage_fit_check.html"

    #: Fallback trial footprint (m) when none is supplied - a mid-size asset.
    DEFAULT_LENGTH = Decimal("8")
    DEFAULT_WIDTH = Decimal("3")

    def _positive_dim(self, name, default):
        raw = self.request.GET.get(name)
        if raw is None:
            return default
        try:
            value = Decimal(raw)
        except InvalidOperation:
            return default
        # NaN/sNaN/Infinity parse fine and then raise on the comparison
        # (QA S-L2: a 500 on ?length=NaN).
        if not value.is_finite():
            return default
        return value if value > 0 else default

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        zones = list(
            StorageZone.objects.filter(is_active=True).prefetch_related(
                "positions__agreement__case__client", "obstacles"
            )
        )
        zone_views = build_zone_views(zones)
        context["zone_views"] = zone_views
        # The same JSON-safe payload the yard plan hands its placer, so
        # the what-if check and the real placement judge a spot by
        # identical rules - including clearance and reserved ground.
        context["yard_data"] = zone_payload(zone_views)
        context["yard_labels"] = yard_labels()
        context["init_length"] = self._positive_dim("length", self.DEFAULT_LENGTH)
        context["init_width"] = self._positive_dim("width", self.DEFAULT_WIDTH)
        return context


#: Who may edit the storage price list - the managerial pair, same as
#: financial settlement (OPEN-05 keeps pricing a management decision).
PRICING_ROLES = SETTLEMENT_ROLES


def _may_edit_pricing(user) -> bool:
    return user.is_superuser or user.role in PRICING_ROLES


class StoragePricingView(StaffRequiredMixin, TemplateView):
    """OPEN-05: the storage price list. Any staff member
    may view it; add/edit/delete are for MANAGER/DIRECTOR."""

    template_name = "backoffice/storage_pricing.html"

    @staticmethod
    def _row_form(form_id, **form_kwargs):
        # The table renders inputs outside their <form> element, so each
        # input carries the HTML form="..." attribute to submit with it.
        form = PriceBandForm(**form_kwargs)
        for field in form.fields.values():
            field.widget.attrs = {**field.widget.attrs, "form": form_id}
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = _may_edit_pricing(self.request.user)
        context["band_groups"] = [
            (
                StorageAccommodation(accommodation).label,
                [
                    (
                        band,
                        self._row_form(
                            "band-form-%s" % band.pk,
                            instance=band,
                            prefix="band-%s" % band.pk,
                        ),
                    )
                    for band in StoragePriceBand.objects.filter(
                        accommodation=accommodation
                    )
                ],
            )
            for accommodation in StorageAccommodation.values
        ]
        context["add_form"] = self._row_form("band-form-new", prefix="new")
        return context


class PricingActionView(StaffRequiredMixin, View):
    """POST-only base for price-list changes; role-gated like
    settlement and redirecting back to the price list either way."""

    redirect_url_name = "backoffice:section-storage-pricing"

    def may_edit(self, user) -> bool:
        """Who may mutate. Overridden where the surface is not a price
        list (the approval ladder is an authorization control, ACT-02)."""
        return _may_edit_pricing(user)

    def denied_message(self, user) -> str:
        return _(
            "Role '%(role)s' may not edit the price list "
            "(allowed: %(allowed)s)."
        ) % {
            "role": user.role or _("(none)"),
            "allowed": ", ".join(PRICING_ROLES),
        }

    def post(self, request, **kwargs):
        if not self.may_edit(request.user):
            messages.error(request, self.denied_message(request.user))
            return redirect(self.redirect_url_name)
        try:
            success = self.perform(request, **kwargs)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, success)
            self.after_success(request)
        return redirect(self.redirect_url_name)

    def after_success(self, request) -> None:
        """Hook run once ``perform`` has saved; no-op by default."""


class PriceBandCreateView(PricingActionView):
    def perform(self, request):
        form = PriceBandForm(request.POST, prefix="new")
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        band = form.save()
        return _("Price band %(band)s added.") % {"band": band}


class PriceBandUpdateView(PricingActionView):
    def perform(self, request, band_pk):
        band = get_object_or_404(StoragePriceBand, pk=band_pk)
        form = PriceBandForm(
            request.POST, instance=band, prefix="band-%s" % band.pk
        )
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        band = form.save()
        return _("Price band %(band)s updated.") % {"band": band}


class PriceBandDeleteView(PricingActionView):
    def perform(self, request, band_pk):
        band = get_object_or_404(StoragePriceBand, pk=band_pk)
        label = str(band)
        band.delete()
        return _("Price band %(band)s deleted.") % {"band": label}


class ServicePricingView(StaffRequiredMixin, TemplateView):
    """The official service price list.
    Any staff member may view it; add/edit/delete are for
    MANAGER/DIRECTOR. Items grouped by category; selected items
    pre-fill quotes on SERVICE cases (ACT-02: suggestion only)."""

    template_name = "backoffice/service_pricing.html"

    @staticmethod
    def _row_form(form_id, **form_kwargs):
        # The table renders inputs outside their <form> element, so each
        # input carries the HTML form="..." attribute to submit with it
        # (the StoragePricingView pattern).
        form = ServicePriceItemForm(**form_kwargs)
        for field in form.fields.values():
            field.widget.attrs = {**field.widget.attrs, "form": form_id}
        return form

    def get_context_data(self, **kwargs):
        from apps.service.models import ServicePriceItem

        context = super().get_context_data(**kwargs)
        context["can_edit"] = _may_edit_pricing(self.request.user)
        context["item_groups"] = [
            (
                ServicePriceItem.Category(category).label,
                [
                    (
                        item,
                        self._row_form(
                            "item-form-%s" % item.pk,
                            instance=item,
                            prefix="item-%s" % item.pk,
                        ),
                    )
                    for item in ServicePriceItem.objects.filter(category=category)
                ],
            )
            for category in ServicePriceItem.Category.values
        ]
        context["add_form"] = self._row_form("item-form-new", prefix="new")
        return context


class ServicePriceItemCreateView(PricingActionView):
    redirect_url_name = "backoffice:section-service-pricing"

    def perform(self, request):
        form = ServicePriceItemForm(request.POST, prefix="new")
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        item = form.save()
        return _("Price item %(item)s added.") % {"item": item}


class ServicePriceItemUpdateView(PricingActionView):
    redirect_url_name = "backoffice:section-service-pricing"

    def perform(self, request, item_pk):
        from apps.service.models import ServicePriceItem

        item = get_object_or_404(ServicePriceItem, pk=item_pk)
        form = ServicePriceItemForm(
            request.POST, instance=item, prefix="item-%s" % item.pk
        )
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        item = form.save()
        return _("Price item %(item)s updated.") % {"item": item}


class ServicePriceItemDeleteView(PricingActionView):
    redirect_url_name = "backoffice:section-service-pricing"

    def perform(self, request, item_pk):
        from apps.service.models import ServicePriceItem

        item = get_object_or_404(ServicePriceItem, pk=item_pk)
        label = str(item)
        item.delete()
        return _("Price item %(item)s deleted.") % {"item": label}


#: OPEN-01: The spec records the director limit as answered (5,000 EUR,
#: 2026-08-14) while the technician and manager amounts are still pending
#: from management, so rows for these roles are badged as placeholders on
#: the configuration page. This tuple is the code-side mirror of that
#: Note: shrink it in the same commit that records management's
#: answer there.
PENDING_MANAGEMENT_ROLES = (Role.TECHNICIAN, Role.MANAGER)


def _ladder_tier_text(role) -> str:
    """ACT-02 in plain language: what the limit on ``role`` binds.

    Built per request rather than at import time so the active language
    applies (``_`` here is non-lazy ``gettext``).
    """
    texts = {
        Role.TECHNICIAN: _(
            "Amounts up to this limit proceed on the service's own authority "
            "- client consent only, no internal approval is required."
        ),
        Role.MANAGER: _(
            "Amounts above the technician limit and up to this one require a "
            "manager approval."
        ),
        Role.DIRECTOR: _(
            "Amounts above the manager limit require a director approval. "
            "This amount is recorded for reference - it is not read as a "
            "ceiling, the director is the last rung."
        ),
    }
    return texts.get(role, "")


class ApprovalLimitsView(StaffRequiredMixin, TemplateView):
    """OPEN-01: the approval-limit ladder (Limiti odobravanja) as a staff
    page instead of admin-only rows.

    The rows drive ``quotes.services.required_approval_levels()``
    (ACT-02, INV-04). Any staff member may view the ladder; add/edit/
    delete are for MANAGER/DIRECTOR, the price-list convention. No amount
    is seeded or prefilled here - the technician and manager amounts are
    still pending from management (OPEN-01).
    """

    template_name = "backoffice/approval_limits.html"

    @staticmethod
    def _row_form(form_id, **form_kwargs):
        # The table renders inputs outside their <form> element, so each
        # input carries the HTML form="..." attribute to submit with it
        # (the StoragePricingView pattern).
        form = ApprovalLimitForm(**form_kwargs)
        for field in form.fields.values():
            field.widget.attrs = {**field.widget.attrs, "form": form_id}
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = may_edit_approval_ladder(self.request.user)
        by_role = {row.role: row for row in ApprovalLimit.objects.all()}
        limit_rows = []
        missing_roles = []
        # Walk the ladder in escalation order, not alphabetically.
        for role in LADDER_ROLES:
            limit = by_role.pop(role, None)
            if limit is None:
                missing_roles.append(Role(role).label)
                continue
            limit_rows.append(
                {
                    "limit": limit,
                    "role_label": limit.get_role_display(),
                    "form": self._row_form(
                        "limit-form-%s" % limit.pk,
                        instance=limit,
                        prefix="limit-%s" % limit.pk,
                    ),
                    "is_pending": role in PENDING_MANAGEMENT_ROLES,
                    "tier_text": _ladder_tier_text(role),
                }
            )
        context["limit_rows"] = limit_rows
        context["missing_roles"] = missing_roles
        # Rows on roles the ladder does not read (a pre-existing FRONT_DESK
        # row, say): shown as ignored configuration rather than hidden.
        context["extra_rows"] = sorted(by_role.values(), key=lambda row: row.role)
        context["add_form"] = self._row_form("limit-form-new", prefix="new")
        # INV-03 attribution: who moved which rung, most recent first.
        context["recent_changes"] = ApprovalLimitChange.objects.select_related(
            "actor"
        )[:10]
        return context


class ApprovalLimitActionView(PricingActionView):
    """POST-only base for approval-ladder changes.

    Differs from the price-list base in the one way that matters: the
    ladder decides who has to approve a quote, so whoever may rewrite it
    can approve past it. Editing is therefore the director's alone
    (``accounts.services.LADDER_EDIT_ROLES``), not the MANAGER/DIRECTOR
    pair that maintains price lists - a manager who could delete the
    director rung and raise their own would self-approve any amount,
    which is exactly what ACT-02 forbids. Every change also leaves an
    append-only attribution row (INV-03).
    """

    redirect_url_name = "backoffice:approval-limits"

    def may_edit(self, user) -> bool:
        return may_edit_approval_ladder(user)

    def denied_message(self, user) -> str:
        return _(
            "Role '%(role)s' may not change the approval limits: the ladder "
            "decides who has to approve a quote, so only %(allowed)s may "
            "edit it (ACT-02)."
        ) % {
            "role": user.role or _("(none)"),
            "allowed": ", ".join(LADDER_EDIT_ROLES),
        }

    def after_success(self, request) -> None:
        # A new ladder can make a SENT quote complete without anyone
        # being allowed to decide on it again (INV-04): promote it now.
        # Only after a saved edit - a refused one changed no limit.
        for quote in accept_fully_approved_quotes(user=request.user):
            messages.info(
                request,
                _("Quote #%(id)s needs no further approval under the new "
                  "limits and has been accepted.") % {"id": quote.pk},
            )


class ApprovalLimitCreateView(ApprovalLimitActionView):
    def perform(self, request):
        form = ApprovalLimitForm(request.POST, prefix="new")
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        limit = form.save()
        record_approval_limit_change(
            role=limit.role,
            action=ApprovalLimitChange.Action.CREATED,
            actor=request.user,
            new_amount=limit.max_amount,
        )
        return _("Approval limit %(limit)s added.") % {"limit": limit}


class ApprovalLimitUpdateView(ApprovalLimitActionView):
    def perform(self, request, limit_pk):
        limit = get_object_or_404(ApprovalLimit, pk=limit_pk)
        previous_role, previous_amount = limit.role, limit.max_amount
        form = ApprovalLimitForm(
            request.POST, instance=limit, prefix="limit-%s" % limit.pk
        )
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        limit = form.save()
        record_approval_limit_change(
            role=limit.role,
            action=ApprovalLimitChange.Action.UPDATED,
            actor=request.user,
            old_amount=previous_amount if limit.role == previous_role else None,
            new_amount=limit.max_amount,
        )
        return _("Approval limit %(limit)s updated.") % {"limit": limit}


class ApprovalLimitDeleteView(ApprovalLimitActionView):
    def perform(self, request, limit_pk):
        limit = get_object_or_404(ApprovalLimit, pk=limit_pk)
        label = str(limit)
        role, amount = limit.role, limit.max_amount
        limit.delete()
        record_approval_limit_change(
            role=role,
            action=ApprovalLimitChange.Action.DELETED,
            actor=request.user,
            old_amount=amount,
        )
        if role == Role.DIRECTOR:
            # There is no rung above the director, and the director amount
            # is never read as a ceiling: nothing escalates, the rung keeps
            # applying with no threshold on the page to show it.
            return _(
                "Approval limit %(limit)s deleted. Nothing escalates: the "
                "director is the last rung and its amount was recorded for "
                "reference only, so quotes above the manager limit still "
                "require a director approval."
            ) % {"limit": label}
        return _(
            "Approval limit %(limit)s deleted; amounts in that range now "
            "escalate to the next level up."
        ) % {"limit": label}
