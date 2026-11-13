"""
WNT-02 "Unos u bazu": record a storage case in the database.

One staff form that creates/links the Client + Asset (through
``link_case``, so the ``raw_*`` audit data is never modified, GEN-03)
and records the storage agreement - ownership (with optional proof
documents/photos), the site the asset currently sits in, and the
selected storage service. The case status is never touched here;
moving to ``contracted`` stays a separate manager action.
"""
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from .intake_common import ClientAssetIntakeForm, create_or_link_client_asset

from apps.attachments.models import VIDEO_EXTENSIONS, Attachment
from apps.attachments.services import attach_file
from apps.backoffice.forms import PHOTO_EXTENSIONS, MultipleFileField
from apps.backoffice.views import StaffRequiredMixin
from apps.cases.models import Case, CaseType
from apps.cases.services import closed_case_message
from apps.portal.forms import OTHER_LOCATION, LOCATION_CHOICES
from apps.storage.models import (
    StorageAccommodation,
    StoragePackage,
    StoragePriceBand,
    StorageServiceType,
    StorageTerm,
)
from apps.storage.services import attach_permit, price_for, record_storage_entry


class StorageIntakeForm(ClientAssetIntakeForm):
    """WNT-02: client + asset + agreement data in one submission.

    Sections already covered by the case (a linked client or asset)
    are dropped by ``ClientAssetIntakeForm``; the asset length always
    stays because it is mandatory for storage cases (WNT-01) and feeds
    the agreement.
    """

    asset_length_m = forms.DecimalField(
        label=_("Asset length (m)"),
        max_digits=5,
        decimal_places=2,
        min_value=0.01,
        help_text=_("Mandatory for storage cases - length drives pricing."),
    )

    ownership = forms.CharField(
        label=_("Ownership"),
        max_length=255,
        help_text=_("Registered owner / ownership note."),
    )
    origin_site = forms.ChoiceField(label=_("Current site / port"))
    origin_site_other = forms.CharField(
        label=_("Other site / port"),
        max_length=255,
        required=False,
        help_text=_('Fill in only if you selected "Other" above.'),
    )
    selected_services = forms.ModelMultipleChoiceField(
        label=_("Selected services"),
        queryset=StorageServiceType.objects.filter(is_active=True),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text=_(
            "Every service the client has chosen - storage, conservation, "
            "a cleaning - not just the headline one."
        ),
    )
    accommodation = forms.ChoiceField(
        label=_("Accommodation"),
        required=False,
        help_text=_("Open yard or indoor hall - drives the price suggestion."),
    )
    term = forms.ChoiceField(
        label=_("Storage term"),
        required=False,
        help_text=_("Daily, monthly or seasonal - drives the price suggestion."),
    )
    package = forms.ModelChoiceField(
        label=_("Package (optional)"),
        queryset=StoragePackage.objects.all(),
        required=False,
    )
    permit_document = forms.FileField(
        label=_("Permit"),
        required=False,
        help_text=_(
            "The document itself. Optional here - the permit number is "
            "what the agreement needs, and the scan often follows by "
            "e-mail - but filed against the agreement so it is findable "
            "as the permit."
        ),
    )
    ownership_documents = MultipleFileField(
        label=_("Ownership documents / photos"),
        required=False,
        help_text=_("Optional proof of ownership - photos or documents."),
    )

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, case=case, **kwargs)

        choose = [("", _("— choose —"))]
        self.fields["accommodation"].choices = choose + list(
            StorageAccommodation.choices
        )
        self.fields["term"].choices = choose + list(StorageTerm.choices)

        # Required only once there is something to choose: an empty
        # catalogue of service types must not make the page unsubmittable.
        service_qs = self.fields["selected_services"].queryset
        self.fields["selected_services"].required = service_qs.exists()
        if service_qs.count() == 1:
            self.fields["selected_services"].initial = [service_qs.first()]

    # ``clean()`` is the base's: client, asset and the "Other" site.


class StorageIntakeView(StaffRequiredMixin, View):
    """Render and process the WNT-02 database-entry form."""

    template_name = "backoffice/storage_intake.html"

    def get(self, request, pk):
        case = self._get_case(pk)
        form = StorageIntakeForm(case=case, initial=self._initial(case))
        return self._render(request, case, form)

    def post(self, request, pk):
        case = self._get_case(pk)
        # INV-15: this page is not a CaseActionView, and it rewrites the
        # length, ownership, site and term - on a closed case too
        # (QA S-H2).
        refusal = closed_case_message(case)
        if refusal is not None:
            messages.error(request, refusal)
            return redirect("backoffice:case-detail", pk=case.pk)
        form = StorageIntakeForm(request.POST, request.FILES, case=case)
        if not form.is_valid():
            return self._render(request, case, form)
        data = form.cleaned_data

        try:
            with transaction.atomic():
                create_or_link_client_asset(case, form, user=request.user)

                entry = dict(
                    asset_length_m=data["asset_length_m"],
                    ownership=data["ownership"],
                    origin_site=data["origin_site"],
                    selected_services=data.get("selected_services"),
                    accommodation=data.get("accommodation", ""),
                    term=data.get("term", ""),
                )
                # A POST without the field says nothing about the
                # package; passing None cleared it (QA S-H2).
                if "package" in request.POST:
                    entry["package"] = data.get("package")
                agreement = record_storage_entry(
                    case, user=request.user, **entry
                )

                permit = data.get("permit_document")
                if permit is not None:
                    attach_permit(
                        agreement, permit, user=request.user
                    )

                uploaded = []
                for upload in data["ownership_documents"]:
                    extension = upload.name.rsplit(".", 1)[-1].lower()
                    if extension in PHOTO_EXTENSIONS:
                        kind = Attachment.Kind.PHOTO
                    elif extension in VIDEO_EXTENSIONS:
                        kind = Attachment.Kind.VIDEO
                    else:
                        kind = Attachment.Kind.DOCUMENT
                    attach_file(
                        case,
                        upload,
                        kind,
                        Attachment.Stage.REPORT,
                        user=request.user,
                    )
                    uploaded.append(upload.name)
                if uploaded:
                    from django.apps import apps as django_apps

                    ActivityLog = django_apps.get_model("cases", "ActivityLog")
                    ActivityLog.objects.create(
                        case=case,
                        author=request.user,
                        kind=ActivityLog.Kind.SYSTEM,
                        text="WNT-02: ownership document(s) attached: %s."
                        % ", ".join(uploaded),
                    )
        except ValidationError as exc:
            if not form.errors:
                # exc may carry a model-field error dict (e.g. attach_file's
                # "file" key) - flatten to messages the form can hold.
                for message in exc.messages:
                    form.add_error(None, message)
            return self._render(request, case, form)

        suggestion = price_for(agreement)
        if suggestion is not None:
            messages.success(
                request,
                _(
                    "Storage data recorded. Suggested base price from the "
                    "price list: %(price)s EUR."
                ) % {"price": suggestion},
            )
        else:
            messages.success(
                request,
                _(
                    "Storage data recorded. The price list offers no price "
                    "for this length/accommodation/term combination - set "
                    "the agreed price manually."
                ),
            )
        return redirect("backoffice:case-detail", pk=case.pk)

    def _get_case(self, pk):
        return get_object_or_404(
            Case.objects.select_related("client", "asset", "status"),
            pk=pk,
            case_type=CaseType.STORAGE,
        )

    def _initial(self, case):
        """Prefill from data already recorded (WNT-01 inquiry, triage)."""
        initial = {}
        agreement = getattr(case, "storage_agreement", None)
        if agreement is not None:
            if agreement.asset_length_m is not None:
                initial["asset_length_m"] = agreement.asset_length_m
            initial["ownership"] = agreement.ownership
            initial["selected_services"] = list(
                agreement.selected_services.values_list("pk", flat=True)
            )
            initial["package"] = agreement.package_id
            initial["accommodation"] = agreement.accommodation
            initial["term"] = agreement.term
            if agreement.origin_site:
                known_ports = {value for value, _label in LOCATION_CHOICES}
                if agreement.origin_site in known_ports:
                    initial["origin_site"] = agreement.origin_site
                else:
                    initial["origin_site"] = OTHER_LOCATION
                    initial["origin_site_other"] = agreement.origin_site
        if "asset_length_m" not in initial and case.asset is not None:
            initial["asset_length_m"] = case.asset.length_m
        return initial

    def _render(self, request, case, form):
        agreement = getattr(case, "storage_agreement", None)
        band_groups = [
            (
                StorageAccommodation(accommodation).label,
                list(StoragePriceBand.objects.filter(accommodation=accommodation)),
            )
            for accommodation in StorageAccommodation.values
        ]
        return render(
            request,
            self.template_name,
            {
                "case": case,
                "form": form,
                "agreement": agreement,
                "band_groups": band_groups,
            },
        )
