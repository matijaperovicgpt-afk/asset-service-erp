"""Domain services for the parts app.

Light inventory logic: ordering parts for a case, marking them received,
and maintaining the catalogue itself (``delete_part``, used by the
backoffice parts page, OPEN-02).
"""
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.cases.services import SETTLEMENT_ROLES

from .models import Part, PartOrder

#: Roles that may declare a part a stocked consumable (SVC-10): the
#: catalogue owners, the same pair that maintains the price lists and
#: settles cases (``SETTLEMENT_ROLES``; the backoffice ``PRICING_ROLES``
#: is the same tuple), so the three rules cannot drift apart. The flag
#: decides the direct route and whether an order owes the 50% advance,
#: so any other role minting one - stock 999, price 0 - could then order
#: it with no advance (QA SV-H4 / R-M1).
STOCKING_ROLES = SETTLEMENT_ROLES

#: INV-05: the advance owed on a non-stock part, as a share of its price.
PART_ADVANCE_PERCENT = 50


def may_stock_parts(user) -> bool:
    """May ``user`` mark a catalogue part as a stocked consumable?"""
    return user is not None and (user.is_superuser or user.role in STOCKING_ROLES)


def create_catalogue_part(
    case, user, *, code: str, name: str, is_consumable: bool, stock_qty, unit_price
) -> Part:
    """
    SVC-10: mint a catalogue row for a part the diagnosis had to describe
    in prose, so the rest of the chain can reference it. Only a stocking
    role's consumable/stock values are honoured; anyone else adds the
    part as an ordinary, unstocked one. Logged on ``case`` (INV-03).
    """
    may_stock = may_stock_parts(user)
    part = Part.objects.create(
        code=code,
        name=name,
        is_consumable=may_stock and is_consumable,
        stock_qty=(stock_qty or 0) if may_stock else 0,
        unit_price=unit_price,
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-10: part '%s' added to the catalogue (consumable: %s, "
            "stock %s, unit price %s)."
            % (
                part.code,
                "yes" if part.is_consumable else "no",
                part.stock_qty,
                part.unit_price if part.unit_price is not None else "-",
            )
        ),
    )
    return part


def part_advance_amount(price) -> Decimal:
    """INV-05: the advance a non-stock part at ``price`` owes, to the cent."""
    return (Decimal(price) * Decimal(PART_ADVANCE_PERCENT) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def order_part_with_advance(case, part: Part, price, expected_delivery=None):
    """
    SVC-10/SVC-11: order ``part`` for ``case`` at the price staff typed.
    For a non-stock part the mandatory 50% advance (INV-05) is derived
    from that price, recorded on the case and linked to the order in the
    same transaction. Returns ``(order, advance_or_None)``.
    """
    from apps.quotes.services import request_advance_payment

    with transaction.atomic():
        advance = None
        if not is_stock_part(part):
            advance = request_advance_payment(
                case, percent=PART_ADVANCE_PERCENT, amount=part_advance_amount(price)
            )
        order = order_part(
            case, part, expected_delivery=expected_delivery, advance_payment=advance
        )
    return order, advance

MIN_ADVANCE_PERCENT = 50


def is_stock_part(part: Part) -> bool:
    """A regular consumable that is currently in stock (SVC-10)."""
    return part.is_consumable and part.stock_qty > 0


def delete_part(part: Part) -> str:
    """
    Remove a catalogue row, refusing to touch case history (INV-03).

    A ``Part`` is referenced by two PROTECTed relations, both of which are
    case history: ``PartOrder`` (the ordering record, SVC-11) and
    ``service.UsedPart`` (the parts installed on a work order, SVC-15).
    Deleting a part that either of them points at would either fail at
    database level or destroy history, so it is refused with a message
    naming the counts.

    It deliberately does NOT advise clearing "consumable" or zeroing the
    stock as a way to retire the row: that is not a retirement (the part
    stays selectable in the part-order form) and it changes SVC-10 for the
    open orders that already point at it, so the case can no longer enter
    "awaiting part" until a 50% advance is recorded (SVC-11/SVC-14). A
    real retirement needs an ``is_active`` flag filtering the order form -
    a schema change and an OPEN-02 decision, not something to improvise in
    an error message.

    Returns the deleted part's label for the caller's success message.
    """
    order_count = part.orders.count()
    used_count = part.used_in.count()
    # SVC-10: a diagnostic naming this part is case history too - it is
    # exactly what the shortcut guard reads. Counted here rather than left
    # to the PROTECT constraint, which reaches the caller as the vaguer
    # "just been referenced" message below.
    needed_count = part.needed_by.count()
    if order_count or used_count or needed_count:
        raise ValidationError(
            _(
                "Part '%(code)s' cannot be deleted: it is referenced by "
                "%(orders)s part order(s), %(used)s work-order "
                "installation(s) and %(needed)s diagnostic(s), which are "
                "case history (INV-03). There is "
                "no way to retire a part yet - do not clear 'consumable' or "
                "zero the stock to fake one, that changes how its open "
                "orders are treated (SVC-10/SVC-11) and still leaves the "
                "part orderable."
            ) % {
                "code": part.code,
                "orders": order_count,
                "used": used_count,
                "needed": needed_count,
            }
        )
    label = str(part)
    try:
        part.delete()
    except ProtectedError:
        # A reference created between the check and the delete: degrade to
        # a message instead of a 500.
        raise ValidationError(
            _(
                "Part '%(code)s' cannot be deleted: it has just been "
                "referenced by case history (INV-03)."
            ) % {"code": part.code}
        )
    return label


def order_part(case, part: Part, expected_delivery=None, advance_payment=None) -> PartOrder:
    """
    Create a ``PartOrder`` for ``case``, enforcing SVC-11 / INV-05: a
    non-stock part may only be ordered with a >=50% advance payment
    recorded on the same case and linked to the order.
    """
    if not is_stock_part(part):
        if advance_payment is None:
            raise ValidationError(
                _(
                    "Part '%(code)s' is not a stocked consumable: a 50%% "
                    "advance payment recorded on the case is required before "
                    "ordering (SVC-11/INV-05)."
                ) % {"code": part.code}
            )
        if advance_payment.case_id != case.pk:
            raise ValidationError(
                _(
                    "Advance payment #%(id)s belongs to case #%(other)s, "
                    "not case #%(case)s."
                ) % {
                    "id": advance_payment.pk,
                    "other": advance_payment.case_id,
                    "case": case.pk,
                }
            )
        if advance_payment.percent < MIN_ADVANCE_PERCENT:
            raise ValidationError(
                _(
                    "Advance payment #%(id)s covers %(percent)s%%; at least "
                    "%(min)s%% is required for a non-stock part (SVC-11)."
                ) % {
                    "id": advance_payment.pk,
                    "percent": advance_payment.percent,
                    "min": MIN_ADVANCE_PERCENT,
                }
            )
    return PartOrder.objects.create(
        case=case,
        part=part,
        expected_delivery=expected_delivery,
        advance_payment=advance_payment,
    )


def receive_part_order(part_order: PartOrder, user=None) -> PartOrder:
    """
    Mark ``part_order`` as received: stamp ``received_at``, bump the
    part's stock by one (light inventory) and log a SYSTEM entry on the
    case.

    Deliberately does NOT transition the case out of ``awaiting_part``:
    whether work resumes is a staff decision made in the backoffice
    (SVC-14 only guards entering the status).
    """
    with transaction.atomic():
        # Re-read under a row lock: three parallel POSTs each saw the
        # order open and bumped the stock three times (QA SV-M2).
        received_at = (
            PartOrder.objects.select_for_update()
            .values_list("received_at", flat=True)
            .get(pk=part_order.pk)
        )
        if received_at is not None:
            raise ValidationError(
                _("Part order #%(id)s was already received at %(when)s.")
                % {"id": part_order.pk, "when": received_at}
            )
        part_order.received_at = timezone.now()
        part_order.save(update_fields=["received_at"])

        # An atomic increment: a read-modify-write lost one of two
        # concurrent receipts for the same part.
        part = part_order.part
        Part.objects.filter(pk=part.pk).update(stock_qty=F("stock_qty") + 1)
        part.refresh_from_db(fields=["stock_qty"])

        still_open = part_order.case.part_orders.filter(
            received_at__isnull=True
        ).count()
        text = "Part order #%s (%s) received." % (part_order.pk, part.code)
        if still_open:
            text += (
                " %s other part order(s) on this case are still open."
                % still_open
            )
        ActivityLog = apps.get_model("cases", "ActivityLog")
        ActivityLog.objects.create(
            case=part_order.case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text=text,
        )
    return part_order
