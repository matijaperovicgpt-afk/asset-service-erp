"""Domain services for asset transport.

Scheduling of departures (standard days are configuration data,
TRP-05/OPEN-04), choice between own vehicle and external carrier, and
delivery confirmation (``mark_delivered`` / ``record_delivery_confirmation``,
TRP-08). The final protocol (TRP-09) reuses the shared handover stack
(``intake.HandoverRecord`` + SVC-18 comparison); closing stays SVC-19.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps as django_apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.cases.locking import lock_row, serialised_per_row
from apps.cases.services import serialised_per_case

from .models import (
    DeliveryConfirmation,
    DeparturePickupSlot,
    LoadingConfirmation,
    LoadingPlan,
    StandardDepartureDay,
    TransportOrder,
    TransportPriceMatrix,
)

# TRP-03: the longest asset the own vehicle may carry (settings / env
# TRANSPORT_OWN_VEHICLE_MAX_LENGTH_M); longer needs an external carrier.
OWN_VEHICLE_MAX_LENGTH_M = settings.TRANSPORT_OWN_VEHICLE_MAX_LENGTH_M


def missing_inquiry_data(order: TransportOrder) -> list:
    """
    TRP-01: which of the inquiry data the procedure names are still
    missing from ``order``.

    "The client provides the asset data, origin site, delivery place
    and preferred date" - the asset, the origin site and the delivery
    place are the three that the offer and the transport are built on, so
    they are the ones a guard can insist on. The desired term is
    deliberately NOT in this list: TRP-05 lets the client choose whether
    they want a predefined slot at all, and the term is settled by the
    departure appointment (``planned_departure_date``) rather than by the
    inquiry. It is reported as a soft warning where it matters instead
    (``ensure_transport_advance``).

    Returned as labels rather than field names because the caller puts
    them in front of staff.
    """
    missing = []
    if not (order.origin or "").strip():
        missing.append(_("the origin site"))
    if not (order.destination or "").strip():
        missing.append(_("the delivery place"))
    if order.asset_length_m is None and getattr(order.case, "asset", None) is None:
        missing.append(_("the asset"))
    return missing


def inquiry_is_documented(order) -> bool:
    """TRP-01: is there enough of an inquiry on file to build an offer on?

    ``None`` (no transport order at all) is not documented.
    """
    if order is None:
        return False
    return not missing_inquiry_data(order)


def carrier_quote_required(order: TransportOrder) -> bool:
    """
    TRP-03: does this transport need the external carrier's own offer?

    "Larger assets are moved by specialized carriers, from whom a
    separate quote is obtained." Either the recorded mode says an external
    carrier is doing it, or the asset is longer than own transport can
    take - the length is authoritative even when the mode was left on the
    default, which is the same reading ``validate_mode`` uses.
    """
    if order is None:
        return False
    if order.mode == TransportOrder.Mode.EXTERNAL_CARRIER:
        return True
    length = order.asset_length_m
    if length is None:
        asset = getattr(order.case, "asset", None)
        length = getattr(asset, "length_m", None)
    return length is not None and Decimal(length) > OWN_VEHICLE_MAX_LENGTH_M


def missing_plan_items(plan) -> list:
    """
    TRP-02: which of the agreed items of the loading/unloading plan are
    still open.

    "Agree the set-down place, crane availability, site conditions, the
    person present at handover and all loading and unloading times."
    Four of the five are concrete facts and are listed here. Site
    conditions is not: it is a free-text note whose honest answer is often
    "nothing worth writing down", and demanding prose produces prose
    rather than agreement. Crane availability counts as open while it is
    still ``TO_ARRANGE`` - "to be arranged" is the absence of the
    agreement, not the agreement.
    """
    if plan is None:
        return [
            _("the unloading location"),
            _("lifting equipment availability"),
            _("the person present at handover"),
            _("the loading time"),
            _("the unloading time"),
        ]
    missing = []
    if not (plan.unloading_location or "").strip():
        missing.append(_("the unloading location"))
    if plan.lifting_availability == LoadingPlan.CraneAvailability.TO_ARRANGE:
        missing.append(_("lifting equipment availability (still 'to be arranged')"))
    if not (plan.attendee or "").strip():
        missing.append(_("the person present at handover"))
    if plan.loading_at is None:
        missing.append(_("the loading time"))
    if plan.unloading_at is None:
        missing.append(_("the unloading time"))
    return missing


def loading_plan_is_agreed(order) -> bool:
    """TRP-02: is the order's loading/unloading plan actually agreed?"""
    if order is None:
        return False
    return not missing_plan_items(getattr(order, "loading_plan", None))


def validate_mode(transport_order: TransportOrder) -> str | None:
    """
    TRP-03 (soft): warn when own-vehicle transport is chosen for an asset
    longer than ~10 m.

    Writes a SYSTEM ``ActivityLog`` entry on the case and returns the
    warning text. Returns ``None`` when the mode/length combination is
    fine or when no asset is linked to the case. This is a soft check:
    it never raises and never blocks saving the order.
    """
    if transport_order.mode != TransportOrder.Mode.OWN_VEHICLE:
        return None

    case = transport_order.case
    asset = getattr(case, "asset", None)
    # TRP-01: the length recorded on the order itself is authoritative for
    # the inquiry; fall back to the linked asset when the order has none.
    length = transport_order.asset_length_m
    if length is None:
        length = asset.length_m if asset is not None else None
    if length is None:
        return None
    if length <= OWN_VEHICLE_MAX_LENGTH_M:
        return None

    label = asset or transport_order.asset_model or _("the asset")
    warning = (
        "TRP-03 warning: own-vehicle transport selected for asset "
        "'%s' with length %s m (own vehicle is only suitable up to "
        "~10 m); an external carrier may be required."
        % (label, length)
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        kind=ActivityLog.Kind.SYSTEM,
        text=warning,
    )
    return warning


def recommended_mode(length_m) -> str | None:
    """
    TRP-03: the transport mode the asset length recommends. Own vehicle
    for assets up to ~10 m (when dimensions and regulations allow),
    otherwise an external specialised carrier. Returns ``None`` when the
    length is unknown, so callers can fall back to a manual choice.

    A pure helper (no side effects) - the case card uses it to preselect
    the mode and to flag a mismatch with an already-chosen mode.
    """
    if length_m is None:
        return None
    if length_m <= OWN_VEHICLE_MAX_LENGTH_M:
        return TransportOrder.Mode.OWN_VEHICLE
    return TransportOrder.Mode.EXTERNAL_CARRIER


def active_departure_day_choices() -> list:
    """TRP-05: form/admin choices sourced from the active configuration
    rows, ordered by weekday."""
    return [
        (day.code, day.name)
        for day in StandardDepartureDay.objects.filter(is_active=True)
    ]


def validate_departure_day(transport_order: TransportOrder) -> str | None:
    """
    TRP-05 / OPEN-04: the set of standard departure days is data.

    HARD: a ``standard_departure_day`` code that is not an active
    ``StandardDepartureDay`` row raises ``ValidationError`` naming the
    allowed codes - the configuration is authoritative.

    SOFT (the TRP-03 pattern): a ``preferred_date`` falling outside the
    standard days writes a SYSTEM ``ActivityLog`` warning and returns
    its text, but never blocks - TRP-05 offers predefined slots, it does
    not forbid an individually agreed date (e.g. external carriers).
    Returns ``None`` when nothing is worth flagging.
    """
    active_days = list(StandardDepartureDay.objects.filter(is_active=True))

    if transport_order.standard_departure_day:
        active_codes = [day.code for day in active_days]
        if transport_order.standard_departure_day not in active_codes:
            raise ValidationError(
                _(
                    "'%(day)s' is not an active standard departure day "
                    "(allowed: %(allowed)s) (TRP-05)."
                ) % {
                    "day": transport_order.standard_departure_day,
                    "allowed": ", ".join(active_codes) or _("none configured"),
                }
            )

    if transport_order.preferred_date is None:
        return None
    active_weekdays = {day.weekday for day in active_days}
    if not active_days or transport_order.preferred_date.weekday() in active_weekdays:
        return None

    warning = (
        "TRP-05 warning: preferred date %s (%s) is not a standard "
        "departure day (%s)."
        % (
            transport_order.preferred_date,
            transport_order.preferred_date.strftime("%A"),
            ", ".join(day.name for day in active_days),
        )
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=transport_order.case,
        kind=ActivityLog.Kind.SYSTEM,
        text=warning,
    )
    return warning


def warn_off_standard_departure(case, departure_date) -> str | None:
    """
    TRP-05: the same soft warning ``validate_departure_day`` writes for
    the desired term, for the term the workflow actually books.

    ``preferred_date`` was watched and the ``TRANSPORT_DEPARTURE``
    appointment was not - yet the appointment is what
    ``planned_departure_date`` reads, what TRP-04 counts its seven days
    back from and what ``_guard_departure_scheduled`` compares against.
    Booking a Monday there left no trace at all. Soft, like its twin: a
    non-standard day is an explicitly sanctioned option under "da li
    zele", not a deviation to refuse.
    """
    if departure_date is None:
        return None
    active_days = list(StandardDepartureDay.objects.filter(is_active=True))
    if not active_days:
        return None
    if departure_date.weekday() in {day.weekday for day in active_days}:
        return None

    warning = (
        "TRP-05 warning: the booked departure %s (%s) is not a standard "
        "departure day (%s)."
        % (
            departure_date,
            departure_date.strftime("%A"),
            ", ".join(day.name for day in active_days),
        )
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        kind=ActivityLog.Kind.SYSTEM,
        text=warning,
    )
    return warning


# TRP-02: the loading/unloading plan fields staff may write incrementally
# (the AGREEMENT_FIELDS / PICKUP_FIELDS convention).
LOADING_PLAN_FIELDS = (
    "unloading_location",
    "lifting_availability",
    "lifting_notes",
    "site_conditions",
    "attendee",
    "loading_at",
    "unloading_at",
    "notes",
)


def upsert_loading_plan(
    transport_order: TransportOrder, *, user=None, **fields
) -> LoadingPlan:
    """
    TRP-02: create or update the order's loading/unloading plan from
    staff-entered values. Only keys present in ``fields`` (and known to
    the plan) are written, so a partially agreed plan is completed
    incrementally rather than overwritten.
    """
    plan, _created = LoadingPlan.objects.get_or_create(transport_order=transport_order)
    for name in LOADING_PLAN_FIELDS:
        if name in fields:
            setattr(plan, name, fields[name])
    plan.save()
    return plan


def record_loading_plan(case, *, user=None, **fields) -> LoadingPlan:
    """
    TRP-02 "Loading and unloading plan": record the loading/unloading plan
    agreed during consultation - unloading location, lifting equipment availability,
    site conditions, the attendee at handover and the loading/unloading
    times - on the case's transport order and log the entry.

    Raises ``ValidationError`` for non-transport cases or when the case
    has no transport order yet (the order carries required data and is
    created earlier in the flow, TRP-01). Delegates the write to
    ``upsert_loading_plan`` so a partially planned load is completed, not
    duplicated.
    """
    if case.case_type != "TRANSPORT":
        raise ValidationError(
            _("Case #%(id)s is not a transport case.") % {"id": case.pk}
        )

    order = getattr(case, "transport_order", None)
    if order is None:
        raise ValidationError(
            _("Case #%(id)s has no transport order.") % {"id": case.pk}
        )

    plan = upsert_loading_plan(order, user=user, **fields)

    # Audit text is stored data - plain strings, not gettext (the
    # record_site_pickup convention).
    parts = []
    if plan.unloading_location:
        parts.append("unloading '%s'" % plan.unloading_location)
    parts.append("crane %s" % plan.get_lifting_availability_display())
    if plan.attendee:
        parts.append("attendee '%s'" % plan.attendee)
    if plan.loading_at:
        parts.append("loading %s" % plan.loading_at)
    if plan.unloading_at:
        parts.append("unloading %s" % plan.unloading_at)
    text = "TRP-02: loading/unloading plan recorded - %s." % (
        ", ".join(parts) or "no details"
    )

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return plan


# TRP-07: the loading-confirmation fields staff fill in at loading (the
# LOADING_PLAN_FIELDS convention). ``signed_by`` / ``signed_at`` are set
# by the service, not passed in.
LOADING_CONFIRMATION_FIELDS = (
    "vehicle_type",
    "readiness",
    "lifting_equipment_used",
    "carrier",
    "client_notified",
    "notes",
)


def upsert_loading_confirmation(
    transport_order: TransportOrder, *, user=None, **fields
) -> LoadingConfirmation:
    """
    TRP-07: create or update the order's loading confirmation from
    staff-entered values. Only keys present in ``fields`` (and known to
    the confirmation) are written. Submitting the confirmation stamps the
    responsible person's sign-off (``signed_by`` / ``signed_at``) - each
    save re-attests the current securing / road-readiness state.
    """
    confirmation, _created = LoadingConfirmation.objects.get_or_create(
        transport_order=transport_order
    )
    for name in LOADING_CONFIRMATION_FIELDS:
        if name in fields:
            setattr(confirmation, name, fields[name])
    if user is not None:
        confirmation.signed_by = user
        confirmation.signed_at = timezone.now()
    confirmation.save()
    return confirmation


def record_loading_confirmation(case, *, user=None, **fields) -> LoadingConfirmation:
    """
    TRP-07 "Loading and carriage": record the INTERNAL loading confirmation -
    truck/trailer, securing/road-readiness, whether a crane was used, the
    carrier present and the responsible person's sign-off - on the case's
    transport order, and log the entry (TRP-07 is "logged as an activity
    entry"). Any client notification happens outside the app; the
    ``client_notified`` flag only records that it was done.

    Raises ``ValidationError`` for non-transport cases or when the case
    has no transport order yet (created earlier in the flow, TRP-01).
    Delegates the write to ``upsert_loading_confirmation`` so a partially
    entered confirmation is completed, not duplicated.
    """
    if case.case_type != "TRANSPORT":
        raise ValidationError(
            _("Case #%(id)s is not a transport case.") % {"id": case.pk}
        )

    order = getattr(case, "transport_order", None)
    if order is None:
        raise ValidationError(
            _("Case #%(id)s has no transport order.") % {"id": case.pk}
        )

    # TRP-06 "Before loading, the asset's condition is recorded...": the intake record
    # is made BEFORE loading, and it is the document that says what the
    # asset looked like before anyone touched it. Recorded afterwards it
    # certifies the opposite of what it is for - damage done by the
    # strap reads as damage that arrived with the asset. Refused here
    # rather than only on the ``in_transit`` guard, because the guard is
    # an existence check that a later-written record satisfies just as
    # well, and because every route into the loading card comes through
    # this service.
    # TRP-07 "The carrier and the responsible person check securing
    # and road-readiness": two parties check, and the record showed one.
    # A third-party carrier cannot sign in this app, so what is asked for
    # is that they are named - and only where there IS a third party. On
    # an own-vehicle job the site is the carrier, and a blank is the
    # truthful answer. Only a READY sign-off is held to it: recording
    # "nothing verified yet" is not a claim about who verified it.
    existing = getattr(order, "loading_confirmation", None)
    readiness = fields.get(
        "readiness", getattr(existing, "readiness", None)
    )
    carrier = fields.get("carrier", getattr(existing, "carrier", "") or "")
    # "Third party" is read the way TRP-03 reads it - by length as well
    # as mode: a 14 m asset left on OWN_VEHICLE with a declared carrier
    # took a READY sign-off with the carrier blank (QA T-L1).
    if (
        readiness == LoadingConfirmation.Readiness.READY
        and carrier_quote_required(order)
        and not (carrier or "").strip()
    ):
        raise ValidationError(
            _(
                "Name the carrier who checked the securing with you - the "
                "carrier and the responsible person verify road-readiness "
                "together (TRP-07)."
            )
        )

    IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
    if not IntakeRecord.objects.filter(
        case=case, signed_at__isnull=False
    ).exists():
        raise ValidationError(
            _(
                "The intake record is made and signed before "
                "loading, not after it - it is what records the asset's "
                "condition before anyone touched it (TRP-06)."
            )
        )

    confirmation = upsert_loading_confirmation(order, user=user, **fields)

    # Audit text is stored data - plain strings, not gettext (the
    # record_loading_plan convention).
    parts = [
        "vehicle %s" % confirmation.get_vehicle_type_display(),
        "readiness %s" % confirmation.get_readiness_display(),
    ]
    if confirmation.lifting_equipment_used:
        parts.append("crane used")
    if confirmation.carrier:
        parts.append("carrier '%s'" % confirmation.carrier)
    if confirmation.client_notified:
        parts.append("client notified (outside app)")
    text = "TRP-07: loading confirmed (internal) - %s." % ", ".join(parts)

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return confirmation


# TRP-08: the delivery-confirmation fields staff fill in at delivery (the
# LOADING_CONFIRMATION_FIELDS convention). ``signed_by`` / ``signed_at``
# are set by the service, not passed in.
DELIVERY_CONFIRMATION_FIELDS = (
    "unloading_done",
    "put_into_service",
    "attendee_role",
    "attendee_name",
    "attendee_contact",
    "accompanying_employee",
    "notes",
)


def upsert_delivery_confirmation(
    transport_order: TransportOrder, *, user=None, **fields
) -> DeliveryConfirmation:
    """
    TRP-08: create or update the order's delivery confirmation from
    staff-entered values. Only keys present in ``fields`` (and known to
    the confirmation) are written. Submitting stamps the staff member's
    sign-off (``signed_by`` / ``signed_at``) - each save re-attests the
    current delivery state (the TRP-07 convention).
    """
    confirmation, _created = DeliveryConfirmation.objects.get_or_create(
        transport_order=transport_order
    )
    for name in DELIVERY_CONFIRMATION_FIELDS:
        if name in fields:
            setattr(confirmation, name, fields[name])
    if user is not None:
        confirmation.signed_by = user
        confirmation.signed_at = timezone.now()
    confirmation.save()
    return confirmation


def record_delivery_confirmation(case, *, user=None, **fields) -> DeliveryConfirmation:
    """
    TRP-08 "Delivery": record the delivery protocol - unloading organized,
    asset put into service, the attending owner or authorized
    representative (a named person is mandatory), the accompanying company
    employee and the staff sign-off - on the case's transport order, and
    log the entry. Also stamps ``delivered_at`` on the order when it is
    not set yet, so one submission records both the protocol and the
    delivery moment (an existing timestamp is left alone).

    Raises ``ValidationError`` for non-transport cases or when the case
    has no transport order yet (created earlier in the flow, TRP-01).
    Delegates the write to ``upsert_delivery_confirmation`` so a partially
    entered confirmation is completed, not duplicated.
    """
    if case.case_type != "TRANSPORT":
        raise ValidationError(
            _("Case #%(id)s is not a transport case.") % {"id": case.pk}
        )

    order = getattr(case, "transport_order", None)
    if order is None:
        raise ValidationError(
            _("Case #%(id)s has no transport order.") % {"id": case.pk}
        )

    # TRP-08: "the owner or an authorized representative attends" - a
    # named attendee is mandatory on the protocol.
    attendee_name = fields.get("attendee_name", "")
    if not (attendee_name or "").strip():
        raise ValidationError(
            _(
                "The person present at delivery is required - the owner or "
                "an authorized representative attends (TRP-08)."
            )
        )

    # TRP-08 "At the delivery place": the protocol attests to something that
    # happened at the delivery place. Signed before the asset has even
    # departed it is a statement about the future, and because
    # ``_guard_delivered`` only asks whether a confirmation exists, one
    # such save satisfies the gate permanently.
    from apps.cases.services import has_reached_status

    if not has_reached_status(case, "in_transit"):
        raise ValidationError(
            _(
                "The delivery protocol is recorded at the delivery place, "
                "so the asset has to have departed first - case #%(id)s "
                "is still at '%(status)s' (TRP-08)."
            )
            % {"id": case.pk, "status": case.status.name}
        )

    # TRP-08 "unloading and putting into service are organized": a delivery where
    # nothing came off the truck is not a delivery. Putting into service is the
    # softer half - delivery onto a trailer or into a yard is legitimate -
    # so it is allowed against a stated reason rather than refused.
    #
    # Read against the record as it will stand, not against the keys of
    # this call: the write is an upsert, so a second, partial submission
    # legitimately omits what the first one already recorded.
    existing = getattr(order, "delivery_confirmation", None)

    def _effective(name):
        if name in fields:
            return fields[name]
        return getattr(existing, name, None)

    if not _effective("unloading_done"):
        raise ValidationError(
            _(
                "A delivery protocol records an unloading that happened - "
                "confirm the asset was unloaded, or do not sign the "
                "protocol yet (TRP-08)."
            )
        )
    if not _effective("put_into_service") and not (_effective("notes") or "").strip():
        raise ValidationError(
            _(
                "The asset was not put into service - state why in "
                "the notes (delivery onto a trailer or into a yard is "
                "legitimate, but it is a deviation worth recording, "
                "TRP-08)."
            )
        )

    confirmation = upsert_delivery_confirmation(order, user=user, **fields)

    delivered_stamped = False
    if order.delivered_at is None:
        order.delivered_at = timezone.now()
        order.save(update_fields=["delivered_at"])
        delivered_stamped = True

    # Audit text is stored data - plain strings, not gettext (the
    # record_loading_confirmation convention).
    parts = []
    if confirmation.unloading_done:
        parts.append("unloaded")
    if confirmation.put_into_service:
        parts.append("put into service")
    parts.append(
        "attendee '%s' (%s)"
        % (confirmation.attendee_name, confirmation.get_attendee_role_display())
    )
    if confirmation.accompanying_employee is not None:
        parts.append(
            "accompanied by '%s'"
            % confirmation.accompanying_employee.get_username()
        )
    if delivered_stamped:
        parts.append("delivered_at stamped")
    text = "TRP-08: delivery confirmed - %s." % ", ".join(parts)

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return confirmation


def has_delivery_protocol(confirmation: DeliveryConfirmation) -> bool:
    """TRP-08: is the signed delivery-protocol scan attached to the
    confirmation? (The ``has_handover_approval`` pattern.)"""
    from apps.attachments.services import attachments_for

    Attachment = django_apps.get_model("attachments", "Attachment")
    return attachments_for(confirmation).filter(
        kind=Attachment.Kind.DOCUMENT
    ).exists()


def attach_delivery_protocol(confirmation: DeliveryConfirmation, file, user):
    """
    TRP-08: the physically signed delivery protocol is a scanned document,
    stored as a DOCUMENT attachment on the confirmation itself (the
    ``attach_handover_approval`` pattern). Re-uploads are allowed.
    """
    from apps.attachments.services import attach_file

    Attachment = django_apps.get_model("attachments", "Attachment")
    attachment = attach_file(
        confirmation,
        file,
        Attachment.Kind.DOCUMENT,
        Attachment.Stage.HANDOVER,
        user=user,
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=confirmation.transport_order.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "TRP-08: signed delivery protocol scan '%s' attached to "
            "delivery confirmation #%s."
            % (attachment.original_name, confirmation.pk)
        ),
    )
    return attachment


@serialised_per_row
def mark_delivered(transport_order: TransportOrder, user, note: str = "") -> TransportOrder:
    """
    TRP-08: record the physical delivery of the asset - timestamp set
    once, double delivery refused. Never transitions the case: moving
    ``in_transit -> delivered`` stays a staff decision through
    ``transition_case()`` (the final protocol/handover follows, TRP-09).
    """
    if transport_order.delivered_at is not None:
        raise ValidationError(
            _("Transport order #%(id)s was already delivered at %(when)s (TRP-08).")
            % {"id": transport_order.pk, "when": transport_order.delivered_at}
        )
    # The stamp is permanent, so it may only record a delivery that can
    # have happened: one stamped at "Upit" predated the offer and was
    # then kept over the real protocol's time (QA T-H4). The same rule
    # ``record_delivery_confirmation`` applies.
    from apps.cases.services import has_reached_status

    case = transport_order.case
    if not has_reached_status(case, "in_transit"):
        raise ValidationError(
            _(
                "The asset has to have departed before it can be "
                "delivered - case #%(id)s is still at '%(status)s' (TRP-08)."
            )
            % {"id": case.pk, "status": case.status.name}
        )
    transport_order.delivered_at = timezone.now()
    update_fields = ["delivered_at"]
    if note:
        transport_order.delivery_note = note
        update_fields.append("delivery_note")
    transport_order.save(update_fields=update_fields)
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=transport_order.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "TRP-08: transport order #%s marked delivered."
            % transport_order.pk
        ),
    )
    return transport_order


def declare_carrier_price(
    transport_order: TransportOrder, price, user, carrier_name: str = ""
) -> TransportOrder:
    """
    TRP-03: record the price of the found external carrier for an asset
    that own-vehicle transport cannot handle (over ~10 m), where no
    standard price list applies and a separate offer is obtained.

    Stamps ``carrier_price_declared_at`` once (a second declaration is
    refused, the ``mark_delivered`` pattern), writes a SYSTEM
    ``ActivityLog`` entry and notifies staff that a carrier price is now
    on file so the workflow can proceed (email + log, mirroring NTF-01;
    there is no SMS gateway). Never transitions the case.

    The check-then-stamp runs under the order's row lock; the notification
    goes out only after that transaction has committed. Without a broker
    the mail is sent inline, and sending it under the lock would hold the
    order row for the whole SMTP round trip - and tell staff about a
    price that a failed commit never stored (the ``place_asset`` split).
    """
    with transaction.atomic():
        lock_row(transport_order)
        _declare_carrier_price_locked(transport_order, price, user, carrier_name)

    from apps.notifications.services import notify_transport_price_declared

    notify_transport_price_declared(transport_order.case)
    return transport_order


def _declare_carrier_price_locked(transport_order, price, user, carrier_name):
    if transport_order.carrier_price_declared_at is not None:
        raise ValidationError(
            _(
                "A carrier price for transport order #%(id)s was already "
                "declared at %(when)s (TRP-03)."
            )
            % {
                "id": transport_order.pk,
                "when": transport_order.carrier_price_declared_at,
            }
        )
    if price is None:
        raise ValidationError(_("A carrier price is required (TRP-03)."))

    transport_order.carrier_price = price
    transport_order.carrier_price_declared_at = timezone.now()
    update_fields = ["carrier_price", "carrier_price_declared_at"]
    if carrier_name:
        transport_order.carrier_name = carrier_name
        update_fields.append("carrier_name")
    transport_order.save(update_fields=update_fields)

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=transport_order.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "TRP-03: external carrier price declared - %s%s (order #%s)."
            % (
                transport_order.carrier_price,
                " via '%s'" % transport_order.carrier_name
                if transport_order.carrier_name
                else "",
                transport_order.pk,
            )
        ),
    )


# --- TRP-01 "Inquiry and offer" -----------------------------------------

# The service scope stated on the offer, per the price list
# (the transport price list). Stored/displayed business data, not
# gettext (the audit-text convention).
STANDARD_SCOPE = (
    "Loading of goods (forklifts, cranes); "
    "export documentation; "
    "import documentation - import guarantee; "
    "transport from the port to the destination; "
    "unloading of goods."
)

# TRP-01: the inquiry fields staff may write incrementally (the
# LOADING_PLAN_FIELDS / AGREEMENT_FIELDS convention). A partial phone
# inquiry is completed over several saves, not overwritten.
ORDER_FIELDS = (
    "asset_brand",
    "asset_model",
    "asset_length_m",
    "origin",
    "destination",
    "destination_zone",
    "preferred_date",
    "pickup_slot",
    "mode",
    "carrier_name",
    "standard_departure_day",
    "scope_of_service",
    "estimated_cost",
)


def upsert_transport_order(case, *, user=None, **fields) -> TransportOrder:
    """
    TRP-01: create or update the case's transport order from staff-entered
    inquiry documentation. Only keys present in ``fields`` (and known to
    the order) are written, so a partial phone inquiry can be completed
    incrementally.
    """
    order, _created = TransportOrder.objects.get_or_create(case=case)
    for name in ORDER_FIELDS:
        if name in fields:
            setattr(order, name, fields[name])
    order.save()
    return order


def price_for(order: TransportOrder) -> Decimal | None:
    """
    TRP-01: suggested transport price for the order's ``asset_model`` and
    ``destination_zone`` from the price matrix
    (the transport price list). Pure read; ``None`` when model or
    zone is missing, no active row matches, or the row is priced
    individually (NULL price = "negotiable", no suggestion). The commercially
    stated estimate stays in ``order.estimated_cost``.
    """
    if not order.asset_model or not order.destination_zone:
        return None
    row = (
        TransportPriceMatrix.objects.filter(
            model_code__iexact=order.asset_model.strip(),
            destination_zone=order.destination_zone,
            is_active=True,
        )
        .order_by("pk")
        .first()
    )
    return row.price if row is not None else None


def record_transport_inquiry(case, *, user=None, **fields) -> TransportOrder:
    """
    TRP-01 "Upit": record the inquiry data the client supplies - asset
    data, origin site, delivery place and desired term - on the case's
    transport order and log the entry. Delegates the write to
    ``upsert_transport_order`` so a partially documented inquiry is
    completed, not duplicated.

    Raises ``ValidationError`` for non-transport cases. Never touches
    ``case.status``.
    """
    if case.case_type != "TRANSPORT":
        raise ValidationError(
            _("Case #%(id)s is not a transport case.") % {"id": case.pk}
        )

    order = upsert_transport_order(case, user=user, **fields)

    # Audit text is stored data - plain strings, not gettext (the
    # record_loading_plan convention).
    parts = []
    if order.asset_brand or order.asset_model:
        parts.append(
            "asset '%s'" % " ".join(
                p for p in (order.asset_brand, order.asset_model) if p
            )
        )
    if order.asset_length_m is not None:
        parts.append("length %s m" % order.asset_length_m)
    if order.origin:
        parts.append("origin '%s'" % order.origin)
    if order.destination:
        parts.append("destination '%s'" % order.destination)
    if order.destination_zone:
        parts.append("zone '%s'" % order.get_destination_zone_display())
    if order.preferred_date is not None:
        parts.append("desired date %s" % order.preferred_date)
    text = "TRP-01: transport inquiry recorded - %s." % (
        ", ".join(parts) or "no details"
    )
    suggestion = price_for(order)
    if suggestion is not None:
        text += " Suggested price from the price list: %s EUR." % suggestion

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return order


def suggest_transport_offer(case) -> dict:
    """
    TRP-01 prefill for the offer form on a transport case. Pure read -
    nothing is written.

    Returns a dict with ``description`` (the standard scope of service),
    ``amount`` (the recorded estimate, else the price-list suggestion) and
    ``suggested_price`` (the price-list value, for surfacing a deviation).
    """
    order = getattr(case, "transport_order", None)
    suggested = price_for(order) if order is not None else None

    amount = None
    if order is not None:
        amount = order.estimated_cost
        if amount is None:
            amount = suggested

    description = order.scope_of_service if order is not None else ""
    if not description:
        description = STANDARD_SCOPE

    return {
        "description": description,
        "amount": amount,
        "suggested_price": suggested,
    }


@transaction.atomic
def create_transport_offer(
    case,
    *,
    description: str,
    amount,
    valid_until=None,
    user=None,
):
    """
    TRP-01 "Offer": draft the quote issued BEFORE any transport is
    organized, stating the scope of service and the estimated cost. The
    scope and the route (origin site -> delivery place, zone) are always
    stated in the quote text. Records the scope and estimate on the order.

    Raises ``ValidationError`` for non-transport cases. Returns the DRAFT
    ``Quote`` (send it with ``quotes.services.send_quote``).
    """
    from apps.quotes.services import create_quote

    if case.case_type != "TRANSPORT":
        raise ValidationError(
            _("Case #%(id)s is not a transport case.") % {"id": case.pk}
        )

    order = upsert_transport_order(
        case, user=user, scope_of_service=description, estimated_cost=amount
    )

    # State the route in the quote itself (the create_storage_offer
    # convention of appending the commercial context to the description).
    route_bits = []
    if order.origin or order.destination:
        route_bits.append(
            _("Route: %(origin)s -> %(destination)s.") % {
                "origin": order.origin or "?",
                "destination": order.destination or "?",
            }
        )
    if order.destination_zone:
        route_bits.append(
            _("Delivery zone: %(zone)s.") % {
                "zone": order.get_destination_zone_display()
            }
        )
    full_description = "\n".join([description, *route_bits]) if route_bits else description

    quote = create_quote(
        case, full_description, amount, user=user, valid_until=valid_until
    )

    # Audit text is stored data - plain strings, not gettext.
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text="TRP-01: transport offer quote #%s issued (%s)." % (
            quote.pk,
            "estimate %s EUR" % amount if amount is not None else "no estimate",
        ),
    )
    return quote


def available_pickup_slots():
    """TRP-01: the saved available pickup times - AVAILABLE slots today or
    later, for offering a departure term to the client."""
    return DeparturePickupSlot.objects.filter(
        status=DeparturePickupSlot.Status.AVAILABLE,
        date__gte=timezone.localdate(),
    )


def validate_pickup_slot(order: TransportOrder) -> str | None:
    """
    TRP-01 (soft, the ``validate_departure_day`` pattern): warn when the
    order's ``preferred_date`` matches no available pickup slot. Writes a
    SYSTEM ``ActivityLog`` entry and returns its text, but never blocks -
    an individually agreed term is allowed. Returns ``None`` when the date
    is empty, matches an available slot, or no slots are published.
    """
    if order.preferred_date is None:
        return None
    slots = available_pickup_slots()
    if not slots.exists() or slots.filter(date=order.preferred_date).exists():
        return None
    warning = (
        "TRP-01 warning: desired date %s matches no available pickup slot."
        % order.preferred_date
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=order.case,
        kind=ActivityLog.Kind.SYSTEM,
        text=warning,
    )
    return warning


# TRP-04: the advance is a fixed 50% of the offer - not a per-case choice,
# so it is derived, never typed.
TRANSPORT_ADVANCE_PERCENT = 50

#: TRP-04: "at least 7 days before the planned departure".
ADVANCE_DUE_DAYS_BEFORE_DEPARTURE = 7


def planned_departure_date(case):
    """TRP-04 departure-date source, mirroring ``_guard_departure_scheduled``:
    the booked ``TRANSPORT_DEPARTURE`` appointment, falling back to the
    order's ``preferred_date``. ``None`` when neither is known."""
    Appointment = django_apps.get_model("scheduling", "Appointment")
    appointment = (
        Appointment.objects.filter(
            case=case, kind=Appointment.Kind.TRANSPORT_DEPARTURE
        )
        .order_by("scheduled_for")
        .first()
    )
    if appointment is not None:
        return timezone.localdate(appointment.scheduled_for)
    order = getattr(case, "transport_order", None)
    if order is not None and order.preferred_date:
        return order.preferred_date
    return None


def derived_advance_amount(quote) -> Decimal:
    """The 50% of ``quote`` that TRP-04 requires, quantized to cents."""
    return (
        Decimal(TRANSPORT_ADVANCE_PERCENT) / Decimal(100) * quote.amount
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def payable_transport_quote(case):
    """The offer the advance is derived from: the *latest* quote, and only
    once it has left DRAFT and was not rejected - the same "latest quote"
    notion the rest of the app uses, so a newer revision silently retires
    the older offer as the advance's basis."""
    Quote = django_apps.get_model("quotes", "Quote")
    quote = case.quotes.order_by("-created_at", "-pk").first()
    if quote is None or quote.status in (Quote.Status.DRAFT, Quote.Status.REJECTED):
        return None
    return quote


@serialised_per_case
def ensure_transport_advance(case, *, user=None):
    """
    TRP-04: keep the mandatory 50% advance on ``case`` derived from the
    offer, so staff never enter an amount.

    Idempotent - safe to call after every send/revision. Creates the
    advance when the case has a payable offer and none exists, and
    re-derives an *unpaid* one when the offer amount changed. A **paid**
    advance is never touched: it is a recorded fact and the
    ``departure_scheduled`` guard (INV-06) depends on it; a revision after
    payment is a settlement question, not an automatic rewrite.

    Returns the ``AdvancePayment``, or ``None`` for a non-transport case
    or one with no payable offer yet.
    """
    if case.case_type != "TRANSPORT":
        return None

    quote = payable_transport_quote(case)
    if quote is None:
        return None

    AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
    paid = case.advance_payments.filter(paid_at__isnull=False).order_by("-paid_at")
    if paid.exists():
        return paid.first()

    amount = derived_advance_amount(quote)
    due_date = None
    departure = planned_departure_date(case)
    if departure is not None:
        due_date = departure - timedelta(days=ADVANCE_DUE_DAYS_BEFORE_DEPARTURE)

    ActivityLog = django_apps.get_model("cases", "ActivityLog")

    # TRP-04: with no planned departure the seven days have nothing to
    # count back from, and the rule silently evaluates to nothing at all.
    # Say so out loud - a soft warning, because the departure is normally
    # booked later and this same function re-derives the due date when it
    # is (``book_appointment`` calls back in).
    if departure is None:
        ActivityLog.objects.create(
            case=case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text=(
                "TRP-04 warning: the advance has no due date - no departure "
                "is planned yet, so \"at least seven days before departure\" "
                "cannot be evaluated. It is set when the departure "
                "appointment is booked."
            ),
        )
    elif due_date is not None and due_date < timezone.localdate():
        ActivityLog.objects.create(
            case=case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text=(
                "TRP-04 warning: the offer was sent less than seven days "
                "before the planned departure on %s - the advance deadline "
                "of %s has already passed." % (departure, due_date)
            ),
        )
    advance = case.advance_payments.order_by("-pk").first()
    if advance is None:
        advance = AdvancePayment.objects.create(
            case=case,
            quote=quote,
            percent=TRANSPORT_ADVANCE_PERCENT,
            amount=amount,
            due_date=due_date,
        )
        ActivityLog.objects.create(
            case=case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text=(
                "TRP-04: advance payment #%s derived from offer #%s - %s%% of "
                "%s = %s%s."
                % (
                    advance.pk,
                    quote.pk,
                    TRANSPORT_ADVANCE_PERCENT,
                    quote.amount,
                    amount,
                    ", due %s" % due_date if due_date else "",
                )
            ),
        )
        return advance

    changed = []
    if advance.quote_id != quote.pk:
        advance.quote = quote
        changed.append("quote")
    if advance.percent != TRANSPORT_ADVANCE_PERCENT:
        advance.percent = TRANSPORT_ADVANCE_PERCENT
        changed.append("percent")
    if advance.amount != amount:
        advance.amount = amount
        changed.append("amount")
    if due_date is not None and advance.due_date != due_date:
        advance.due_date = due_date
        changed.append("due_date")
    if changed:
        advance.save(update_fields=changed)
        ActivityLog.objects.create(
            case=case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text=(
                "TRP-04: advance payment #%s re-derived from offer #%s - %s%% "
                "of %s = %s (%s)."
                % (
                    advance.pk,
                    quote.pk,
                    TRANSPORT_ADVANCE_PERCENT,
                    quote.amount,
                    amount,
                    ", ".join(changed),
                )
            ),
        )
    return advance
