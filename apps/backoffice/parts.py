"""
OPEN-02: the backoffice parts catalogue (Katalog dijelova).

``Part.is_consumable`` together with ``stock_qty`` decides whether a part
counts as stock (``parts.services.is_stock_part``, SVC-10) and therefore
whether ordering it needs a 50% advance (SVC-11/INV-05) before a case may
enter ``awaiting_part`` (SVC-14). The mechanism has been final since
2026-08-01, but the rows were editable only in the Django admin, so this
page gives management the same list/add/edit/delete surface the price
lists have (the ``TransportPricingView`` pattern, itself a copy of
``views.StoragePricingView``).

No parts are seeded and no part is declared a consumable here: the
initial consumables list is still pending from management (OPEN-02). The
catalogue legitimately starts empty.
"""
from django import forms
from django.apps import apps as django_apps
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from .formmixins import BootstrapFormMixin

from apps.backoffice.views import (
    PRICING_ROLES,
    StaffRequiredMixin,
    _may_edit_pricing,
)
from apps.parts.models import Part
from apps.parts.services import delete_part, is_stock_part


class PartForm(BootstrapFormMixin, forms.ModelForm):
    """One catalogue row. ``code`` is unique, so a duplicate surfaces as a
    form error rather than an integrity error.

    ACT-02 puts the consumable decision with management, so the flag is
    editable here rather than only in the admin (OPEN-02); which parts are
    consumables is not decided by this form.

    ``unit_price`` is here for the same reason: SVC-14 totals the work
    done against the approved amount, and a part with no price stops that
    check rather than silently counting as free - so the people who can
    fix it need somewhere to fix it.
    """

    class Meta:
        model = Part
        fields = ("code", "name", "is_consumable", "stock_qty", "unit_price")

    def clean_stock_qty(self):
        # ``stock_qty`` is a plain IntegerField (a DB-level validator would
        # need a migration); ``is_stock_part`` already treats anything
        # below 1 as "not in stock", so a negative is only ever confusing.
        qty = self.cleaned_data["stock_qty"]
        if qty is not None and qty < 0:
            raise ValidationError(_("Stock quantity cannot be negative."))
        return qty


class PartCatalogView(StaffRequiredMixin, TemplateView):
    """OPEN-02: the parts catalogue. Any staff member may view it;
    add/edit/delete are for MANAGER/DIRECTOR (ACT-02: the manager decides
    whether a part counts as a consumable)."""

    template_name = "backoffice/parts_catalog.html"

    @staticmethod
    def _row_form(form_id, **form_kwargs):
        # Inputs render outside their <form>, so each carries form="..."
        # (the StoragePricingView pattern).
        form = PartForm(**form_kwargs)
        for field in form.fields.values():
            field.widget.attrs = {**field.widget.attrs, "form": form_id}
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_edit"] = _may_edit_pricing(self.request.user)
        parts = (
            Part.objects.annotate(
                # distinct=True on every aggregate: the two reverse joins
                # would otherwise multiply each other's counts.
                order_count=Count("orders", distinct=True),
                open_order_count=Count(
                    "orders",
                    filter=Q(orders__received_at__isnull=True),
                    distinct=True,
                ),
                used_count=Count("used_in", distinct=True),
            )
            .order_by("code")
        )
        context["part_rows"] = [
            {
                "part": part,
                "is_stock": is_stock_part(part),
                "order_count": part.order_count,
                "open_order_count": part.open_order_count,
                "used_count": part.used_count,
                "is_referenced": bool(part.order_count or part.used_count),
                "form": self._row_form(
                    "part-form-%s" % part.pk,
                    instance=part,
                    prefix="part-%s" % part.pk,
                ),
            }
            for part in parts
        ]
        # The INV-03 "cannot be deleted" explanation is one footnote under
        # the table, not a paragraph in every referenced row.
        context["any_referenced"] = any(
            row["is_referenced"] for row in context["part_rows"]
        )
        context["add_form"] = self._row_form("part-form-new", prefix="new")
        return context


class PartCatalogActionView(StaffRequiredMixin, View):
    """POST-only base for catalogue changes; role-gated like the price
    lists and redirecting back to the catalogue either way."""

    redirect_url_name = "backoffice:parts-catalog"

    def post(self, request, **kwargs):
        if not _may_edit_pricing(request.user):
            messages.error(
                request,
                _(
                    "Role '%(role)s' may not edit the parts catalogue "
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


def _stock_status_text(part: Part) -> str:
    if is_stock_part(part):
        return _("It counts as a stocked consumable (SVC-10).")
    return _("It does not count as a stocked consumable (SVC-10).")


class PartCreateView(PartCatalogActionView):
    def perform(self, request):
        form = PartForm(request.POST, prefix="new")
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        part = form.save()
        return _("Part %(part)s added. %(status)s") % {
            "part": part,
            "status": _stock_status_text(part),
        }


def _log_stock_change(request, part, was_qty) -> None:
    """Record a hand-edited stock quantity where it can be found later.

    ``receive_part_order`` logs every unit it adds; this form could set the
    same number to anything and left no trace at all - so the one place
    stock moves for a documented reason was logged and the one place it
    moves arbitrarily was not.

    ``ActivityLog`` is case-scoped, so there is nowhere case-free to write
    to. The line goes on the cases the change actually affects - the two
    places this number is read at transition time:

    * a case with an open order for the part (SVC-11: whether ordering it
      needed a 50% advance, re-evaluated when the case is moved), and
    * a case whose diagnostic names the part (SVC-10: whether the store
      holds enough of it for the direct route to ``in_progress``).

    A part nothing points at yet still changes silently. That is the limit
    of a case-scoped log, not a decision - the alternative is a catalogue
    audit table, which is more than this fix.
    """
    cases = {
        order.case_id: order.case
        for order in part.orders.filter(
            received_at__isnull=True
        ).select_related("case")
    }
    cases.update(
        {
            needed.diagnostic.work_order.case_id: needed.diagnostic.work_order.case
            for needed in part.needed_by.select_related(
                "diagnostic__work_order__case"
            )
        }
    )
    if not cases:
        return
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.bulk_create(
        [
            ActivityLog(
                case=case,
                author=request.user,
                kind=ActivityLog.Kind.SYSTEM,
                text=(
                    "Stock quantity for part %s set from %s to %s in the "
                    "catalogue. It counts as a stocked consumable "
                    "(SVC-10): %s."
                    % (
                        part.code,
                        was_qty,
                        part.stock_qty,
                        "yes" if is_stock_part(part) else "no",
                    )
                ),
            )
            for case in cases.values()
        ]
    )


class PartUpdateView(PartCatalogActionView):
    def perform(self, request, part_pk):
        part = get_object_or_404(Part, pk=part_pk)
        was_stock = is_stock_part(part)
        was_qty = part.stock_qty
        form = PartForm(request.POST, instance=part, prefix="part-%s" % part.pk)
        if not form.is_valid():
            raise ValidationError(form.errors.as_text())
        part = form.save()
        if part.stock_qty != was_qty:
            _log_stock_change(request, part, was_qty)
        now_stock = is_stock_part(part)
        if now_stock != was_stock:
            # The change is management's call (ACT-02) and is not blocked,
            # but SVC-10/SVC-11/SVC-14 outcomes flip with it - including
            # for part orders that are already open, because the guard
            # evaluates is_stock_part() at transition time.
            open_orders = part.orders.filter(received_at__isnull=True).count()
            if now_stock:
                messages.warning(
                    request,
                    _(
                        "Part %(part)s now counts as a stocked consumable "
                        "(SVC-10): ordering it no longer requires a 50%% "
                        "advance. Open part order(s) on it: %(open)s."
                    ) % {"part": part, "open": open_orders},
                )
            else:
                messages.warning(
                    request,
                    _(
                        "Part %(part)s no longer counts as a stocked "
                        "consumable (SVC-10): its %(open)s open part order(s) "
                        "now need a 50%% advance recorded on the case "
                        "(SVC-11/INV-05) before it may enter 'awaiting part' "
                        "(SVC-14)."
                    ) % {"part": part, "open": open_orders},
                )
        return _("Part %(part)s updated.") % {"part": part}


class PartDeleteView(PartCatalogActionView):
    def perform(self, request, part_pk):
        part = get_object_or_404(Part, pk=part_pk)
        # Refuses parts referenced by case history (INV-03).
        label = delete_part(part)
        return _("Part %(part)s deleted.") % {"part": label}
