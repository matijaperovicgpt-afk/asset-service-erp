"""
TRP-01 "Inquiry and offer": the backoffice pages for the transport inquiry.

Three surfaces mirroring the storage flow:

* ``TransportIntakeView`` - one staff form that creates/links the
  Client + Asset (through ``link_case``, so the ``raw_*`` audit data is
  never modified, GEN-03) and records the inquiry (asset data, origin
  site, delivery place + zone, desired term) via
  ``record_transport_inquiry``.
* ``TransportPricingView`` + the ``TransportPrice*`` actions - the
  transport price list as an editable matrix; add/edit/delete
  are for MANAGER/DIRECTOR (the storage-pricing convention).
* ``TransportOfferCreateView`` - drafts the offer quote with scope and
  estimated cost via ``create_transport_offer``.
"""
from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from .formmixins import BootstrapFormMixin
from .forms import QuoteCreateForm
from .intake_common import ClientAssetIntakeForm, create_or_link_client_asset

from apps.backoffice.views import (
    PRICING_ROLES,
    CaseActionView,
    FormInvalid,
    StaffRequiredMixin,
    _may_edit_pricing,
    closed_case_message,
)
from apps.cases.services import has_reached_status
from apps.cases.models import Case, CaseType
from apps.portal.forms import OTHER_LOCATION, LOCATION_CHOICES
from apps.transport.models import (
    DeparturePickupSlot,
    DestinationZone,
    TransportOrder,
    TransportPriceMatrix,
)
from apps.transport.services import (
    active_departure_day_choices,
    available_pickup_slots,
    create_transport_offer,
    price_for,
    record_transport_inquiry,
    recommended_mode,
    suggest_transport_offer,
    validate_departure_day,
    validate_mode,
    validate_pickup_slot,
)


class TransportIntakeForm(ClientAssetIntakeForm):
    """TRP-01: client + asset + inquiry data in one submission.

    The client / asset sections (and dropping them when the case already
    covers them) come from ``ClientAssetIntakeForm``; the fields here
    capture the inquiry values so every one is remembered on the
    transport order.
    """

    asset_brand = forms.CharField(
        label=_("Asset brand"),
        max_length=100,
        required=False,
        help_text=_("Manufacturer or brand."),
    )
    asset_model = forms.CharField(
        label=_("Asset model"),
        max_length=100,
        required=False,
        help_text=_("Model code from the price list, e.g. 'M20' (drives pricing)."),
    )
    asset_length_m = forms.DecimalField(
        label=_("Asset length (m)"),
        max_digits=5,
        decimal_places=2,
        min_value=0.01,
        help_text=_("Length drives the own-vehicle mode check."),
    )

    origin_site = forms.ChoiceField(label=_("Origin site / port"))
    origin_site_other = forms.CharField(
        label=_("Other site / port"),
        max_length=255,
        required=False,
        help_text=_('Fill in only if you selected "Other" above.'),
    )
    destination = forms.CharField(
        label=_("Delivery place"),
        max_length=255,
        help_text=_("Where the asset is delivered."),
    )
    destination_zone = forms.ChoiceField(
        label=_("Delivery zone"),
        required=False,
        help_text=_("Delivery zone - drives the price suggestion."),
    )
    preferred_date = forms.DateField(
        label=_("Desired date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    standard_departure_day = forms.ChoiceField(
        label=_("Standard departure day"),
        required=False,
        help_text=_(
            "Fill in only if the client chose one of the predefined "
            "departure days rather than a date of their own."
        ),
    )
    pickup_slot = forms.ModelChoiceField(
        label=_("Available pickup slot"),
        queryset=DeparturePickupSlot.objects.none(),
        required=False,
        help_text=_("Pick a published available time, if any."),
    )
    mode = forms.ChoiceField(label=_("Mode"), required=False)

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, case=case, **kwargs)

        choose = [("", _("— choose —"))]
        self.fields["destination_zone"].choices = choose + list(
            DestinationZone.choices
        )
        self.fields["mode"].choices = choose + list(TransportOrder.Mode.choices)
        # TRP-05: the days are configuration, so the choices come from the
        # active rows - the same source ``validate_departure_day`` checks
        # against, which is what keeps the two from disagreeing.
        self.fields["standard_departure_day"].choices = [
            ("", _("— a date of their own —"))
        ] + active_departure_day_choices()
        slots = available_pickup_slots()
        # The booked slot has to stay in the queryset even after it drops out
        # of the available set (date passed, status changed). Without it the
        # bound value is rejected as an invalid choice and the term the
        # client already agreed to cannot be re-selected from this screen.
        booked = getattr(
            getattr(case, "transport_order", None), "pickup_slot_id", None
        )
        if booked is not None:
            slots = DeparturePickupSlot.objects.filter(
                Q(pk__in=slots.values("pk")) | Q(pk=booked)
            )
        self.fields["pickup_slot"].queryset = slots
        if booked is None and not slots.exists():
            # Slots are published from the admin. With none published this
            # was an empty select with no explanation - a control the user
            # could see, click, and get nothing from.
            self.fields["pickup_slot"].help_text = _(
                "No pickup slots are published at the moment - leave this "
                "empty and agree the time with the client directly."
            )
            # Deliberately not `disabled`: a disabled select submits nothing
            # and the view writes back whatever comes in, so disabling it
            # would erase a slot recorded earlier.
            self.fields["pickup_slot"].widget.attrs["readonly"] = True

    def clean(self):
        # Client, asset and the "Other" site are settled by the base.
        cleaned = super().clean()

        # TRP-03: an untouched Mode select used to be collapsed to
        # "own vehicle" by the view, so the case page stated a mode nobody
        # chose - and stated it wrongly for anything over ~10 m. Derive it
        # from the length instead, which is the rule the warning checks.
        if not cleaned.get("mode"):
            recommended = recommended_mode(cleaned.get("asset_length_m"))
            if recommended:
                cleaned["mode"] = recommended
            else:
                self.add_error("mode", _("Choose the transport mode."))
        return cleaned


class TransportIntakeView(StaffRequiredMixin, View):
    """Render and process the TRP-01 inquiry form."""

    template_name = "backoffice/transport_intake.html"

    def get(self, request, pk):
        case = self._get_case(pk)
        form = TransportIntakeForm(case=case, initial=self._initial(case))
        return self._render(request, case, form)

    def post(self, request, pk):
        case = self._get_case(pk)
        # INV-15 plus a status gate: the inquiry is what the offer and the
        # departure were planned on. It rewrote closed cases (and the
        # shared asset length), and switching to an external carrier in
        # transit dodged TRP-03, which is checked at departure (QA T-H3).
        refusal = closed_case_message(case)
        if refusal is None and has_reached_status(case, "departure_scheduled"):
            refusal = _(
                "The departure of case #%(id)s is already scheduled - the "
                "inquiry it was planned on can no longer be changed "
                "(TRP-01)."
            ) % {"id": case.pk}
        if refusal is not None:
            messages.error(request, refusal)
            return redirect("backoffice:case-detail", pk=case.pk)
        form = TransportIntakeForm(request.POST, case=case)
        if not form.is_valid():
            return self._render(request, case, form)
        data = form.cleaned_data

        try:
            with transaction.atomic():
                create_or_link_client_asset(case, form, user=request.user)

                order = record_transport_inquiry(
                    case,
                    user=request.user,
                    asset_brand=data.get("asset_brand", ""),
                    asset_model=data.get("asset_model", ""),
                    asset_length_m=data["asset_length_m"],
                    origin=data["origin_site"],
                    destination=data["destination"],
                    destination_zone=data.get("destination_zone", ""),
                    preferred_date=data.get("preferred_date"),
                    standard_departure_day=data.get(
                        "standard_departure_day", ""
                    ),
                    pickup_slot=data.get("pickup_slot"),
                    mode=data["mode"],
                )
        except ValidationError as exc:
            if not form.errors:
                for message in exc.messages:
                    form.add_error(None, message)
            return self._render(request, case, form)

        # TRP-01/03/05: these three checks were written as soft validators
        # but were wired only into TransportOrderAdmin.save_model, a screen
        # staff never open - so an own-vehicle 14 m inquiry saved silently.
        # Run them outside the atomic block, as the admin does, so their
        # ActivityLog rows survive.
        self._warn(request, order)

        suggestion = price_for(order)
        if suggestion is not None:
            messages.success(
                request,
                _(
                    "Transport inquiry recorded. Suggested price from the "
                    "price list: %(price)s EUR."
                ) % {"price": suggestion},
            )
        else:
            messages.success(
                request,
                _(
                    "Transport inquiry recorded. The price list offers no "
                    "price for this model/zone (negotiable) - set the estimate "
                    "manually in the offer."
                ),
            )
        return redirect("backoffice:case-detail", pk=case.pk)

    def _warn(self, request, order):
        """Run the TRP-03/05/01 soft checks and show what they return."""
        warnings = []
        for check in (validate_mode, validate_pickup_slot):
            warning = check(order)
            if warning:
                warnings.append(warning)
        try:
            warning = validate_departure_day(order)
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            if warning:
                warnings.append(warning)
        for warning in warnings:
            messages.warning(request, warning)

    def _get_case(self, pk):
        return get_object_or_404(
            Case.objects.select_related("client", "asset", "status"),
            pk=pk,
            case_type=CaseType.TRANSPORT,
        )

    def _initial(self, case):
        initial = {}
        order = getattr(case, "transport_order", None)
        if order is not None:
            initial.update(
                asset_brand=order.asset_brand,
                asset_model=order.asset_model,
                asset_length_m=order.asset_length_m,
                destination=order.destination,
                destination_zone=order.destination_zone,
                preferred_date=order.preferred_date,
                standard_departure_day=order.standard_departure_day,
                mode=order.mode,
                pickup_slot=order.pickup_slot_id,
            )
            # origin_site is required, so leaving it out of the prefill
            # made every re-save fail validation; and because the view
            # writes pickup_slot unconditionally, the second attempt wiped
            # the booked term. Map the stored string back onto the choice
            # the same way the storage intake does.
            if order.origin:
                known_ports = {value for value, _label in LOCATION_CHOICES}
                if order.origin in known_ports:
                    initial["origin_site"] = order.origin
                else:
                    initial["origin_site"] = OTHER_LOCATION
                    initial["origin_site_other"] = order.origin
        if order is None and case.asset is not None:
            initial["asset_length_m"] = case.asset.length_m
        return initial

    def _render(self, request, case, form):
        return render(
            request,
            self.template_name,
            {"case": case, "form": form},
        )


class TransportPriceMatrixForm(BootstrapFormMixin, forms.ModelForm):
    """A single row of the transport price matrix (TRP-01)."""

    class Meta:
        model = TransportPriceMatrix
        fields = ("brand", "model_code", "destination_zone", "price", "is_active")

    def clean(self):
        """One row per model and zone, whatever the typing.

        The database constraint is on the raw ``model_code``, so ``M20``
        and ``m20`` are two legal rows - but ``services.price_for`` looks
        the model up with ``__iexact`` and takes the first by pk, so which
        of the two prices a job would be decided by insertion order, with
        nothing on the page saying so. Refused here rather than by a
        constraint: an existing row keeps the casing it was seeded with.
        """
        cleaned = super().clean()
        code = (cleaned.get("model_code") or "").strip()
        zone = cleaned.get("destination_zone")
        if not code or not zone:
            return cleaned
        clash = (
            TransportPriceMatrix.objects.filter(
                model_code__iexact=code, destination_zone=zone
            )
            .exclude(pk=self.instance.pk)
            .first()
        )
        if clash is not None:
            self.add_error(
                "model_code",
                _(
                    "Model '%(code)s' is already priced for this zone as "
                    "'%(existing)s' (row #%(id)s). The price list matches "
                    "the model regardless of capitalisation, so a second "
                    "spelling would only make which price applies depend on "
                    "which row was entered first - edit that row instead."
                ) % {
                    "code": code,
                    "existing": clash.model_code,
                    "id": clash.pk,
                },
            )
        return cleaned


class TransportPricingView(StaffRequiredMixin, TemplateView):
    """TRP-01: the transport price list. Any staff member may
    view it; add/edit/delete are for MANAGER/DIRECTOR."""

    template_name = "backoffice/transport_pricing.html"

    @staticmethod
    def _row_form(form_id, **form_kwargs):
        # Inputs render outside their <form>, so each carries form="..."
        # (the StoragePricingView pattern).
        form = TransportPriceMatrixForm(**form_kwargs)
        for field in form.fields.values():
            field.widget.attrs = {**field.widget.attrs, "form": form_id}
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = _may_edit_pricing(self.request.user)
        context["price_groups"] = [
            (
                DestinationZone(zone).label,
                [
                    (
                        row,
                        self._row_form(
                            "price-form-%s" % row.pk,
                            instance=row,
                            prefix="price-%s" % row.pk,
                        ),
                    )
                    for row in TransportPriceMatrix.objects.filter(
                        destination_zone=zone
                    )
                ],
            )
            for zone in DestinationZone.values
        ]
        context["add_form"] = self._row_form("price-form-new", prefix="new")
        return context


class TransportPriceActionView(StaffRequiredMixin, View):
    """POST-only base for transport price-list changes; role-gated and
    redirecting back to the price list either way."""

    redirect_url_name = "backoffice:section-transport-pricing"

    def post(self, request, **kwargs):
        if not _may_edit_pricing(request.user):
            messages.error(
                request,
                _(
                    "Role '%(role)s' may not edit the price list "
                    "(allowed: %(allowed)s)."
                ) % {
                    "role": request.user.role or _("(none)"),
                    "allowed": ", ".join(PRICING_ROLES),
                },
            )
            return redirect(self.redirect_url_name)
        try:
            success = self.perform(request, **kwargs)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            messages.success(request, success)
        return redirect(self.redirect_url_name)


class TransportPriceCreateView(TransportPriceActionView):
    def perform(self, request):
        form = TransportPriceMatrixForm(request.POST, prefix="new")
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        row = form.save()
        return _("Price row %(row)s added.") % {"row": row}


class TransportPriceUpdateView(TransportPriceActionView):
    def perform(self, request, row_pk):
        row = get_object_or_404(TransportPriceMatrix, pk=row_pk)
        form = TransportPriceMatrixForm(
            request.POST, instance=row, prefix="price-%s" % row.pk
        )
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        row = form.save()
        return _("Price row %(row)s updated.") % {"row": row}


class TransportPriceDeleteView(TransportPriceActionView):
    def perform(self, request, row_pk):
        row = get_object_or_404(TransportPriceMatrix, pk=row_pk)
        label = str(row)
        row.delete()
        return _("Price row %(row)s deleted.") % {"row": label}


class TransportOfferForm(QuoteCreateForm):
    """TRP-01: the offer quote fields - scope of service and estimate.

    Inherits the quote rules (amount ceiling, no back-dated validity)
    from ``QuoteCreateForm``; only the labels differ."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["description"].label = _("Scope of service")
        self.fields["description"].widget.attrs["rows"] = 4
        self.fields["amount"].label = _("Estimated cost (EUR)")


class TransportOfferCreateView(CaseActionView):
    """TRP-01 "Offer": draft the offer quote with scope and estimated
    cost; the route/zone text lives in ``create_transport_offer``."""

    def perform(self, request, case):
        form = TransportOfferForm(request.POST)
        if not form.is_valid():
            raise FormInvalid("transport_offer_form", form)
        quote = create_transport_offer(
            case,
            description=form.cleaned_data["description"],
            amount=form.cleaned_data["amount"],
            valid_until=form.cleaned_data["valid_until"],
            user=request.user,
        )
        return _("Offer quote #%(id)s created (%(amount)s %(currency)s).") % {
            "id": quote.pk,
            "amount": quote.amount,
            "currency": quote.currency,
        }


def transport_offer_prefill(case) -> dict:
    """Prefill dict for the offer form on a transport case detail page."""
    return suggest_transport_offer(case)
