"""
Triage view: match a case's raw submission data to client/asset records.

Staff see the case's raw_* fields next to ranked suggestions from
``apps.clients.services.suggest_matches`` and either link an existing
client/asset or create a new one; both paths go through ``link_case``
so the raw audit data is never modified (GEN-03).
"""
from decimal import Decimal

from django import forms
from .formmixins import BootstrapFormMixin

from apps.backoffice.views import StaffRequiredMixin
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.cases.models import Case, Status
from apps.cases.services import closed_case_message
from apps.clients.models import Client, Asset
from apps.clients.services import link_case, suggest_matches
from apps.service.services import ensure_preliminary_work_order


class NewClientForm(BootstrapFormMixin, forms.Form):
    """Create a new client (and optionally its asset) during triage."""
    # A full-page form, not one of the dense case-detail cards.
    bootstrap_size = None


    name = forms.CharField(label=_("Name"), max_length=255)
    phone = forms.CharField(label=_("Phone"), max_length=50, required=False)
    email = forms.EmailField(label=_("Email"), required=False)
    asset_name = forms.CharField(label=_("Asset name"), max_length=255, required=False)
    asset_registration = forms.CharField(
        label=_("Asset registration number"), max_length=50, required=False
    )
    # Asset.length_m is NOT NULL - required whenever an asset is created.
    # The case-create form's floor: an asset is at least a centimetre
    # long (QA UI-M3 created one at -1.00 m here).
    asset_length_m = forms.DecimalField(
        label=_("Asset length (m)"),
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        wants_asset = any(
            cleaned.get(f)
            for f in ("asset_name", "asset_registration", "asset_length_m")
        )
        if wants_asset:
            if not cleaned.get("asset_name"):
                self.add_error("asset_name", _("Asset name is required."))
            if cleaned.get("asset_length_m") in (None, ""):
                self.add_error(
                    "asset_length_m", _("Asset length is required to create an asset.")
                )
        return cleaned


class TriageView(StaffRequiredMixin, View):
    """Show raw case data with match suggestions; link or create a client."""

    template_name = "backoffice/triage.html"

    def get(self, request, pk):
        case = get_object_or_404(Case, pk=pk)
        return self._render(request, case, NewClientForm())

    def post(self, request, pk):
        case = get_object_or_404(Case, pk=pk)
        refusal = self._refusal(case)
        if refusal is not None:
            messages.error(request, refusal)
            return redirect("backoffice:case-detail", pk=case.pk)
        action = request.POST.get("action")

        if action == "link":
            client = get_object_or_404(Client, pk=request.POST.get("client_id"))
            asset = None
            asset_id = request.POST.get("asset_id")
            if asset_id:
                # Scoped to the client: somebody else's asset is a forged
                # form, not a match, and gets a 404 rather than the
                # ValueError link_case raises for it.
                asset = get_object_or_404(Asset, pk=asset_id, client=client)
            self._link(request, case, client, asset)
            return self._linked(request, case, client)

        form = NewClientForm(request.POST)
        if form.is_valid():
            client = Client.objects.create(
                name=form.cleaned_data["name"],
                phone=form.cleaned_data["phone"],
                email=form.cleaned_data["email"],
            )
            asset = None
            if form.cleaned_data["asset_name"]:
                asset = Asset.objects.create(
                    client=client,
                    name=form.cleaned_data["asset_name"],
                    asset_type="",
                    registration_no=form.cleaned_data["asset_registration"],
                    length_m=form.cleaned_data["asset_length_m"],
                )
            self._link(request, case, client, asset)
            return self._linked(request, case, client)

        return self._render(request, case, form)

    @staticmethod
    def _refusal(case):
        """Triage answers "whose report is this?" once, at the start.

        It re-linked any case, a closed one included, to another client
        and dropped its asset; a case well into its workflow could be
        moved silently the same way (QA SV-H3). Linking an unlinked case,
        or re-linking one still at its first status, is what triage is
        for; anything later is a correction that needs a person to look
        at what was already recorded under the old client.
        """
        message = closed_case_message(case)
        if message is not None:
            return message
        if case.client_id is None:
            return None
        first = (
            Status.objects.filter(case_type=case.case_type)
            .order_by("sort_order")
            .first()
        )
        if first is not None and case.status_id != first.pk:
            return _(
                "Case #%(id)s is already linked to a client and past its "
                "first step ('%(status)s'); triage no longer moves it to "
                "another client."
            ) % {"id": case.pk, "status": case.status.name}
        return None

    @staticmethod
    def _linked(request, case, client):
        # Back to the case the user was working on, with a word about
        # what happened (QA UI-M3, SV-L1: it landed on the case list).
        messages.success(
            request,
            _("Case #%(id)s linked to client %(client)s.")
            % {"id": case.pk, "client": client.name},
        )
        return redirect("backoffice:case-detail", pk=case.pk)

    @staticmethod
    def _link(request, case, client, asset):
        """Confirm the match, then act on it (SVC-02).

        Triage is the moment a case first has a known client - GEN-03
        keeps the raw report unattached until a human approves the
        match - so it is where the procedure's "if the client is known"
        becomes true and the preliminary work order is opened.
        ``link_case`` itself stays what its docstring says it is: two
        foreign keys and a log line, no workflow side effects.
        """
        link_case(case, client, asset=asset, user=request.user)
        ensure_preliminary_work_order(case, request.user)

    def _render(self, request, case, form):
        return render(
            request,
            self.template_name,
            {
                "case": case,
                "suggestions": suggest_matches(case),
                "form": form,
            },
        )
