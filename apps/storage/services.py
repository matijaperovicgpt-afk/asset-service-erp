"""Domain services for storage ("storage").

Pricing by asset length (``StoragePriceBand``, OPEN-05), season
handling (STORAGE_SEASON_START to the same day next year), and hall placement planning.
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.cases.locking import lock_row
from .models import (
    SEASON_START_DAY as models_SEASON_START_DAY,
    SEASON_START_MONTH as models_SEASON_START_MONTH,
    SitePickup,
    StorageAgreement,
    StorageOrientation,
    StoragePosition,
    StoragePriceBand,
    StorageZone,
)

# Positions without a planned exit date are treated as "leaves last".
_LEAVES_LAST = datetime.date.max


def validate_band(accommodation, length_min_m, length_max_m, exclude_pk=None) -> None:
    """
    OPEN-05: price bands cover ``[min, max)`` and must not overlap
    within one accommodation (open yard and indoor hall are priced
    independently, so the same band may exist in both).

    Raises ``ValidationError`` when min >= max or when the range overlaps
    another band of the same accommodation (a NULL max counts as
    +infinity), naming the conflicting band. ``exclude_pk`` allows
    re-validating an existing band.
    """
    if length_max_m is not None and length_min_m >= length_max_m:
        raise ValidationError(
            _(
                "Invalid band: length_min_m (%(min)s) must be below "
                "length_max_m (%(max)s)."
            ) % {"min": length_min_m, "max": length_max_m}
        )

    # Overlap iff new_min < other_max AND other_min < new_max,
    # with NULL max = +infinity on either side.
    others = StoragePriceBand.objects.filter(accommodation=accommodation)
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    others = others.filter(
        Q(length_max_m__isnull=True) | Q(length_max_m__gt=length_min_m)
    )
    if length_max_m is not None:
        others = others.filter(length_min_m__lt=length_max_m)
    conflict = others.order_by("length_min_m").first()
    if conflict is not None:
        raise ValidationError(
            _("Band [%(min)s, %(max)s) overlaps existing band %(band)s (#%(id)s).")
            % {
                "min": length_min_m,
                "max": length_max_m if length_max_m is not None else "∞",
                "band": conflict,
                "id": conflict.pk,
            }
        )


def band_for(length_m, accommodation) -> StoragePriceBand | None:
    """Return the ``accommodation`` band covering ``length_m`` (min
    inclusive, max exclusive, NULL max open-ended), or ``None`` when no
    band matches."""
    return (
        StoragePriceBand.objects.filter(
            accommodation=accommodation, length_min_m__lte=length_m
        )
        .filter(Q(length_max_m__isnull=True) | Q(length_max_m__gt=length_m))
        .order_by("length_min_m")
        .first()
    )


def price_for(agreement: StorageAgreement) -> Decimal | None:
    """
    OPEN-05 / WNT-01: suggested base storage price for the agreement's
    ``asset_length_m``, ``accommodation`` and ``term`` from the
    price-band matrix. Pure read; ``None`` when length, accommodation or
    term is missing, no band covers the length, or the price list does
    not offer that combination (no suggestion). The commercially agreed
    price stays in ``agreement.agreed_price``.
    """
    if (
        agreement.asset_length_m is None
        or not agreement.accommodation
        or not agreement.term
    ):
        return None
    band = band_for(agreement.asset_length_m, agreement.accommodation)
    return band.price_for_term(agreement.term) if band is not None else None


AGREEMENT_FIELDS = (
    "permit_no",
    "asset_length_m",
    "asset_width_m",
    "origin_site",
    "ownership",
    "package",
    "accommodation",
    "term",
    "agreed_price",
    "period_start",
    "period_end",
    "is_renewal",
    "prior_agreement",
)


#: Sanity ceilings for typed dimensions (m). The largest asset the yard
#: prices is under 14 m; these only stop typing accidents such as a
#: 999.99 m outline or a 30 m breadth on an 8 m asset (QA S-M4).
MAX_ASSET_LENGTH_M = Decimal("60")


def _validate_agreement_values(agreement: StorageAgreement, before=None) -> None:
    """Values no agreement may hold, whichever form wrote them (QA S-M4).

    ``before`` is ``(permit_no, asset_length_m)`` as stored: the
    contracted check refuses *clearing* them, not their never having been
    typed (the transition guard owns that).
    """
    errors = []
    if agreement.agreed_price is not None and agreement.agreed_price <= 0:
        errors.append(_("The agreed price has to be greater than zero."))
    length = agreement.asset_length_m
    width = agreement.asset_width_m
    if length is not None and length > MAX_ASSET_LENGTH_M:
        errors.append(
            _("An asset length of %(len)s m is not plausible (at most %(max)s m).")
            % {"len": length, "max": MAX_ASSET_LENGTH_M}
        )
    if length is not None and width is not None and width > length:
        errors.append(
            _("The breadth (%(w)s m) cannot be larger than the length (%(l)s m).")
            % {"w": width, "l": length}
        )
    # WNT-01: contracting was agreed on these two; blanking them
    # afterwards left a contracted case the guard would never have let
    # through (the guard only runs on the transition).
    from apps.cases.services import has_reached_status

    old_permit, old_length = before or ("", None)
    if has_reached_status(agreement.case, "contracted"):
        if (old_permit or "").strip() and not (
            agreement.permit_no or ""
        ).strip():
            errors.append(
                _("The case is contracted - the permit number cannot be cleared (WNT-01).")
            )
        if old_length is not None and length is None:
            errors.append(
                _("The case is contracted - the asset length cannot be cleared (WNT-01).")
            )
    if errors:
        raise ValidationError(errors)


def _revalidate_yard_position(agreement: StorageAgreement) -> None:
    """A placed asset whose dimensions change must still fit its ground.

    Clearing the length made the asset invisible to every collision check,
    and lengthening it produced an overlap nothing reported (QA S-H4).
    """
    if agreement.pk is None:
        return
    position = agreement.positions.filter(zone__isnull=False).first()
    if position is None:
        return
    if agreement.asset_length_m is None:
        raise ValidationError(
            _(
                "The asset is placed on the yard; its length cannot be "
                "cleared while it holds ground there - lift it out first "
                "(WNT-05)."
            )
        )
    length, width = asset_footprint_size(agreement)
    footprint = zone_footprint(
        length, width, position.pos_x_m, position.pos_y_m, position.orientation
    )
    try:
        validate_free_placement(
            position.zone,
            footprint,
            position.planned_exit_date,
            exclude_pk=position.pk,
        )
    except ValidationError as exc:
        raise ValidationError(
            _(
                "With the new dimensions the asset no longer fits where it "
                "stands on the yard: %(why)s Move it first (WNT-05)."
            ) % {"why": " ".join(exc.messages)}
        ) from exc


def upsert_agreement(case, *, user=None, **fields) -> StorageAgreement:
    """
    WNT-01/WNT-02: create or update the case's storage agreement from
    staff-entered inquiry documentation. Only keys present in ``fields``
    (and known to the agreement) are written, so a partial phone inquiry
    can be completed incrementally.
    """
    agreement, _created = StorageAgreement.objects.get_or_create(case=case)
    before = (agreement.asset_length_m, agreement.asset_width_m)
    stored = (agreement.permit_no, agreement.asset_length_m)
    for name in AGREEMENT_FIELDS:
        if name in fields:
            setattr(agreement, name, fields[name])
    _validate_agreement_values(agreement, stored)
    if (agreement.asset_length_m, agreement.asset_width_m) != before:
        _revalidate_yard_position(agreement)
    agreement.save()
    # WNT-02: a many-to-many cannot be assigned before the row exists, and
    # it is set separately from AGREEMENT_FIELDS so that omitting the key
    # still means "leave it alone" rather than "clear it" - the same
    # incremental contract the scalar fields have.
    if "selected_services" in fields:
        agreement.selected_services.set(fields["selected_services"] or [])
    return agreement


def record_storage_entry(case, *, user=None, **fields) -> StorageAgreement:
    """
    WNT-02 "Unos u bazu": record the storage-agreement data (length,
    site location, ownership, selected service, package) on the case
    and log the entry. Delegates the write to ``upsert_agreement`` so a
    partially documented inquiry (WNT-01) is completed, not duplicated.

    Raises ``ValidationError`` for non-storage cases. Never
    touches ``case.status`` or the ``raw_*`` audit fields.
    """
    from django.apps import apps as django_apps

    if case.case_type != "STORAGE":
        raise ValidationError(
            _("Case #%(id)s is not a storage case.") % {"id": case.pk}
        )

    agreement = upsert_agreement(case, user=user, **fields)

    # Audit text is stored data - plain strings, not gettext (the
    # record_price_deviation / link_case convention).
    parts = []
    if agreement.asset_length_m is not None:
        parts.append("length %s m" % agreement.asset_length_m)
    if agreement.origin_site:
        parts.append("site '%s'" % agreement.origin_site)
    if agreement.ownership:
        parts.append("ownership '%s'" % agreement.ownership)
    services = list(agreement.selected_services.all())
    if services:
        parts.append(
            "services %s" % ", ".join("'%s'" % service for service in services)
        )
    if agreement.package is not None:
        parts.append("package '%s'" % agreement.package)
    if agreement.accommodation:
        parts.append("accommodation '%s'" % agreement.accommodation)
    if agreement.term:
        parts.append("term '%s'" % agreement.term)
    text = "WNT-02: storage agreement data recorded - %s." % (
        ", ".join(parts) or "no details"
    )
    suggestion = price_for(agreement)
    if suggestion is not None:
        text += " Suggested base price from the price list: %s EUR." % suggestion

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return agreement


# ---------------------------------------------------------------------------
# WNT-01 (procedure §5.1): the permit itself, not only its number
# ---------------------------------------------------------------------------

def attach_permit(agreement: StorageAgreement, file, user=None):
    """
    WNT-01: "The client is asked for contact details, the **operating
    permit** and basic asset data."

    The agreement records the permit's reference NUMBER, which is what a
    client can quote over the phone and what the contract cites. The
    document itself is filed here, as a DOCUMENT attachment on the
    agreement - the SVC-05 client-approval pattern - so it is findable as
    the permit rather than as one more report-stage file among the
    ownership photos.

    Advisory, not a gate: the number is the hard requirement, because the
    scan routinely follows by e-mail days after the inquiry and refusing
    to contract over it would stop work the client has already agreed to.
    """
    from apps.attachments.services import attach_file
    from django.apps import apps as django_apps

    Attachment = django_apps.get_model("attachments", "Attachment")
    attachment = attach_file(
        agreement,
        file,
        Attachment.Kind.DOCUMENT,
        Attachment.Stage.REPORT,
        user=user,
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=agreement.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "WNT-01: permit '%s' filed on the storage agreement."
            % attachment.original_name
        ),
    )
    return attachment


def has_permit(agreement: StorageAgreement) -> bool:
    """WNT-01: is the permit document on file (as opposed to its number)?"""
    from apps.attachments.services import attachments_for

    return attachments_for(agreement).exists()


# WNT rule 4 "Pickup from site": the pickup fields staff may write
# incrementally (the AGREEMENT_FIELDS convention).
PICKUP_FIELDS = (
    "organized_by",
    "is_own_site",
    "lift_out_method",
    "lift_out_date",
    "transport_mode",
    "carrier_name",
    "scheduled_date",
    "completed_at",
    "status",
    "intake_record",
    "notes",
)


def upsert_site_pickup(
    agreement: StorageAgreement, *, user=None, **fields
) -> SitePickup:
    """
    WNT rule 4: create or update the agreement's site pickup from
    staff-entered logistics. Only keys present in ``fields`` (and known to
    the pickup) are written, so a partially planned pickup is completed
    incrementally. When the pickup is marked ``COMPLETED`` and no
    completion time is on file, it is stamped now.
    """
    pickup, _created = SitePickup.objects.get_or_create(agreement=agreement)
    for name in PICKUP_FIELDS:
        if name in fields:
            setattr(pickup, name, fields[name])
    if pickup.status == SitePickup.Status.COMPLETED:
        # WNT-05 (procedure §5.4): the pickup happens "with the intake record,
        # condition inventory and photos". A pickup that reports itself complete
        # while pointing at no intake record is claiming paperwork that
        # may not exist - and the inventory and photos hang off that
        # record, so this one link is what makes the other two findable.
        if pickup.intake_record_id is None:
            raise ValidationError(
                _(
                    "The pickup cannot be completed without the intake "
                    "record documenting it - the condition list and the "
                    "photos hang off it (WNT-05)."
                )
            )
        if pickup.completed_at is None:
            pickup.completed_at = timezone.now()
    pickup.save()
    return pickup


def record_site_pickup(case, *, user=None, **fields) -> SitePickup:
    """
    WNT rule 4 "Pickup from site": record how the asset is picked up
    from its site - who organizes it (the service, since most clients
    have no trailer), the lift-out and the road delivery
    from the firm's own or another site - on the storage
    agreement and log the entry. The receipt, condition
    inventory and photos stay on the shared intake stack
    (``intake.IntakeRecord`` / ``IntakeItem`` / arrival ``Attachment``s),
    linked through ``SitePickup.intake_record``.

    Raises ``ValidationError`` for non-storage cases. Delegates the
    write to ``upsert_site_pickup`` so a partially planned pickup is
    completed, not duplicated.
    """
    from django.apps import apps as django_apps

    if case.case_type != "STORAGE":
        raise ValidationError(
            _("Case #%(id)s is not a storage case.") % {"id": case.pk}
        )

    agreement, _created = StorageAgreement.objects.get_or_create(case=case)
    pickup = upsert_site_pickup(agreement, user=user, **fields)

    # Audit text is stored data - plain strings, not gettext (the
    # record_storage_entry convention).
    parts = ["organized by %s" % pickup.get_organized_by_display()]
    site = agreement.origin_site or "?"
    parts.append(
        "site '%s' (%s)"
        % (site, "own" if pickup.is_own_site else "other")
    )
    if pickup.lift_out_method:
        parts.append("lift-out %s" % pickup.get_lift_out_method_display())
    if pickup.transport_mode:
        parts.append("transport %s" % pickup.get_transport_mode_display())
    if pickup.carrier_name:
        parts.append("carrier '%s'" % pickup.carrier_name)
    parts.append("status %s" % pickup.get_status_display())
    text = "WNT rule 4: site pickup recorded - %s." % ", ".join(parts)

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return pickup


# WNT-03: the two offer flavours delivered to the client.
OFFER_STANDALONE = "standalone"
OFFER_PACKAGE = "package"

# WNT-04: the package season runs from STORAGE_SEASON_START to the same day next year. Defined on
# the model (StoragePackage.clean validates against it) and re-exported
# here, where every caller already looks for it.
SEASON_START_MONTH = models_SEASON_START_MONTH
SEASON_START_DAY = models_SEASON_START_DAY


def current_season(today: datetime.date | None = None) -> tuple:
    """
    WNT-04: the commercial season containing ``today`` (default: today).
    Seasons run from STORAGE_SEASON_START to the same day next year, so a date before 15 Oct
    belongs to the season that started the previous year. Returns
    ``(start, end)`` dates.

    "Today" is ``timezone.localdate()``, i.e. today in ``TIME_ZONE``
    (Europe/Belgrade) - not ``date.today()``, which is the server's date.
    On a UTC-hosted server the two differ between 00:00 and 02:00 local
    time, and around 15 October that would put an offer in the wrong
    season.
    """
    if today is None:
        today = timezone.localdate()
    year = today.year
    if (today.month, today.day) < (SEASON_START_MONTH, SEASON_START_DAY):
        year -= 1
    start = datetime.date(year, SEASON_START_MONTH, SEASON_START_DAY)
    end = datetime.date(year + 1, SEASON_START_MONTH, SEASON_START_DAY)
    return start, end


#: From this many days before a season ends, an offer is for the next one.
OFFER_NEXT_SEASON_WITHIN_DAYS = 90


def offer_season(today: datetime.date | None = None) -> tuple:
    """
    WNT-04: the season an offer made ``today`` is for. Usually the running
    one, but close to its end the running season is nearly over: on
    28 Sep the default was 15.10.2025 - 15.10.2026, so a package sold that
    day expired in 17 days (QA S-M3).
    """
    if today is None:
        today = timezone.localdate()
    start, end = current_season(today)
    if (end - today).days <= OFFER_NEXT_SEASON_WITHIN_DAYS:
        start, end = current_season(end)
    return start, end


#: Longest commercial period an offer may state (a season plus slack).
MAX_OFFER_PERIOD_DAYS = 400


def validate_offer_period(period_start, period_end) -> None:
    """WNT-04: the period printed in the client-facing quote has to be a
    real one. An inverted period, one ending in 2226 and a start with no
    end ("15.10.2026. - ?.") were all accepted (QA S-M2)."""
    if period_start is None and period_end is None:
        return
    if period_start is None or period_end is None:
        raise ValidationError(
            _("State both ends of the commercial period, or neither (WNT-04).")
        )
    if period_end <= period_start:
        raise ValidationError(
            _("The commercial period has to end after it starts (WNT-04).")
        )
    if (period_end - period_start).days > MAX_OFFER_PERIOD_DAYS:
        raise ValidationError(
            _(
                "A commercial period of more than %(days)s days is not a "
                "storage season (WNT-04)."
            ) % {"days": MAX_OFFER_PERIOD_DAYS}
        )


def prior_package_agreement(case) -> StorageAgreement | None:
    """
    WNT-03: the client's most recent *other* storage agreement that had a
    package - the signal that this is an existing package user who should
    be offered a renewal ("produzenje"). ``None`` for cases without a
    linked client and for first-time package clients.
    """
    if case.client_id is None:
        return None
    return (
        StorageAgreement.objects.filter(
            case__client_id=case.client_id, package__isnull=False
        )
        .exclude(case_id=case.pk)
        .select_related("package", "case")
        .order_by("-pk")
        .first()
    )


def _package_contents(package) -> str:
    """Comma-separated component names of ``package`` (WNT-04), falling
    back to the free-text ``included_services``."""
    names = [
        component.name
        for component in package.components.filter(is_active=True)
    ]
    return ", ".join(names) if names else package.included_services


def suggest_offer(case) -> dict:
    """
    WNT-03 prefill for the offer form on a storage case: an
    existing package user is suggested a renewal of their previous
    package, a new client gets the (default) package as the recommended
    option. Pure read - nothing is written.

    Returns a dict with ``is_renewal``, ``prior_agreement``, ``package``,
    ``period_start``/``period_end`` (the current season), ``description``
    (prefill text naming the package and its contents) and ``amount``
    (the agreed price when recorded, else the price-list suggestion).
    """
    from .models import StoragePackage

    agreement = getattr(case, "storage_agreement", None)
    prior = prior_package_agreement(case)
    period_start, period_end = offer_season()

    package = agreement.package if agreement is not None else None
    if package is None and prior is not None:
        package = prior.package
    if package is None:
        package = StoragePackage.objects.order_by("pk").first()

    description = ""
    if package is not None:
        if prior is not None:
            description = _(
                "Renewal of package arrangement '%(package)s'."
            ) % {"package": package.name}
        else:
            description = _(
                "Package arrangement '%(package)s' - recommended option."
            ) % {"package": package.name}
        contents = _package_contents(package)
        if contents:
            description += " " + _("Includes: %(contents)s.") % {
                "contents": contents
            }

    amount = None
    if agreement is not None:
        amount = agreement.agreed_price
        if amount is None:
            amount = price_for(agreement)

    return {
        "is_renewal": prior is not None,
        "prior_agreement": prior,
        "package": package,
        "period_start": period_start,
        "period_end": period_end,
        "description": description,
        "amount": amount,
    }


def create_storage_offer(
    case,
    *,
    offer_type: str,
    description: str,
    amount,
    package=None,
    period_start: datetime.date | None = None,
    period_end: datetime.date | None = None,
    valid_until: datetime.date | None = None,
    user=None,
):
    """
    WNT-03 "Offer": draft the quote for a standalone storage service or
    a package arrangement and record the choice on the agreement.

    The commercial period defaults to the current season (WNT-04,
    STORAGE_SEASON_START to the same day next year) and is always stated
    in the quote text, as the rule
    requires period and price in the quote and contract. A package offer
    to a client who already had a package is recorded as a renewal
    ("produzenje") linking back to the prior agreement; a package offer
    to a new client is logged as the recommended option.

    Raises ``ValidationError`` for non-storage cases, unknown
    offer types and package offers without a package. Returns the DRAFT
    ``Quote`` (send it with ``quotes.services.send_quote``).
    """
    from django.apps import apps as django_apps

    from apps.quotes.services import create_quote

    if case.case_type != "STORAGE":
        raise ValidationError(
            _("Case #%(id)s is not a storage case.") % {"id": case.pk}
        )
    if offer_type not in (OFFER_STANDALONE, OFFER_PACKAGE):
        raise ValidationError(
            _("Unknown offer type: %(type)s.") % {"type": offer_type}
        )
    if offer_type == OFFER_PACKAGE and package is None:
        raise ValidationError(
            _("A package offer needs a package (WNT-03).")
        )

    if period_start is None and period_end is None:
        period_start, period_end = offer_season()
    validate_offer_period(period_start, period_end)

    prior = prior_package_agreement(case)
    is_renewal = offer_type == OFFER_PACKAGE and prior is not None

    upsert_agreement(
        case,
        user=user,
        package=package if offer_type == OFFER_PACKAGE else None,
        period_start=period_start,
        period_end=period_end,
        is_renewal=is_renewal,
        prior_agreement=prior if is_renewal else None,
    )

    # WNT-04: the valid commercial period is stated in the quote itself.
    if period_start is not None or period_end is not None:
        description = "%s\n%s" % (
            description,
            _("Commercial period: %(start)s - %(end)s.") % {
                "start": period_start.strftime("%d.%m.%Y.")
                if period_start is not None
                else "?",
                "end": period_end.strftime("%d.%m.%Y.")
                if period_end is not None
                else "?",
            },
        )

    quote = create_quote(
        case, description, amount, user=user, valid_until=valid_until
    )

    # Audit text is stored data - plain strings, not gettext (the
    # record_storage_entry convention).
    if offer_type == OFFER_PACKAGE:
        text = "WNT-03: package offer quote #%s - package '%s', period %s - %s." % (
            quote.pk,
            package,
            period_start,
            period_end,
        )
        if is_renewal:
            text += " Renewal of package agreement #%s." % prior.pk
        else:
            text += " Package recommended to a new client."
    else:
        text = (
            "WNT-03: standalone storage offer quote #%s, "
            "period %s - %s." % (quote.pk, period_start, period_end)
        )

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return quote


def record_price_deviation(agreement: StorageAgreement, user=None) -> str | None:
    """
    OPEN-05: an agreed price deviating from the price-list suggestion is
    never blocked, but the deviation is logged as a SYSTEM activity-log
    entry on the case. Returns the note (for surfacing to staff) or
    ``None`` when there is nothing to report.
    """
    from django.apps import apps as django_apps

    suggestion = price_for(agreement)
    if suggestion is None or agreement.agreed_price is None:
        return None
    if agreement.agreed_price == suggestion:
        return None
    note = (
        "OPEN-05: agreed price %s deviates from the price-list "
        "suggestion %s for asset length %s m (%s, %s)."
        % (
            agreement.agreed_price,
            suggestion,
            agreement.asset_length_m,
            agreement.accommodation,
            agreement.term,
        )
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=agreement.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=note,
    )
    return note


def _depth_key(slot) -> tuple:
    """
    Sort key for how deep a slot is parked (WNT-06 convention: within the
    same hall+row, higher slot numbers are parked deeper, i.e. blocked by
    lower slots). Numeric slots compare numerically; non-numeric slots
    sort after numeric ones, lexicographically.
    """
    text = str(slot).strip()
    try:
        return (0, int(text), "")
    except ValueError:
        return (1, 0, text)


def _exit_key(planned_exit_date) -> datetime.date:
    return planned_exit_date if planned_exit_date is not None else _LEAVES_LAST


def validate_placement(hall, row, slot, planned_exit_date, exclude_pk=None):
    """
    WNT-06: assets that leave first must be reachable. An asset parked
    deeper (higher slot) must not plan to leave before any asset parked
    in front of it (lower slot) in the same hall+row.

    Raises ``ValidationError`` naming the conflicting position; returns
    ``None`` when the placement is valid. ``exclude_pk`` allows
    re-validating an existing position without it conflicting with itself.
    """
    new_depth = _depth_key(slot)
    new_exit = _exit_key(planned_exit_date)

    existing = StoragePosition.objects.filter(hall=hall, row=row)
    if exclude_pk is not None:
        existing = existing.exclude(pk=exclude_pk)

    for position in existing:
        other_depth = _depth_key(position.slot)
        other_exit = _exit_key(position.planned_exit_date)
        if other_depth == new_depth:
            raise ValidationError(
                _(
                    "Slot %(hall)s/%(row)s/%(slot)s is already occupied by "
                    "position #%(id)s (agreement #%(agreement)s)."
                ) % {
                    "hall": hall,
                    "row": row,
                    "slot": slot,
                    "id": position.pk,
                    "agreement": position.agreement_id,
                }
            )
        if new_depth > other_depth and new_exit < other_exit:
            raise ValidationError(
                _(
                    "WNT-06: cannot place asset at %(hall)s/%(row)s/%(slot)s "
                    "(planned exit %(exit)s): it would be parked deeper than "
                    "position #%(id)s at %(position)s, which leaves later "
                    "(planned exit %(other_exit)s) and would block it."
                ) % {
                    "hall": hall,
                    "row": row,
                    "slot": slot,
                    "exit": planned_exit_date,
                    "id": position.pk,
                    "position": position,
                    "other_exit": position.planned_exit_date
                    or _("none - leaves last"),
                }
            )
        if new_depth < other_depth and new_exit > other_exit:
            raise ValidationError(
                _(
                    "WNT-06: cannot place asset at %(hall)s/%(row)s/%(slot)s "
                    "(planned exit %(exit)s): it would block position #%(id)s "
                    "at %(position)s parked deeper, which leaves earlier "
                    "(planned exit %(other_exit)s)."
                ) % {
                    "hall": hall,
                    "row": row,
                    "slot": slot,
                    "exit": planned_exit_date or _("none - leaves last"),
                    "id": position.pk,
                    "position": position,
                    "other_exit": position.planned_exit_date,
                }
            )


def assign_position(
    agreement: StorageAgreement,
    hall: str,
    row: str,
    slot,
    planned_exit_date: datetime.date | None = None,
) -> StoragePosition:
    """
    Validate a placement against WNT-06 and record the ``StoragePosition``.

    Raises ``ValidationError`` (naming the conflicting position) when the
    exit order would trap an asset; otherwise saves and returns the
    position.

    Upserts the agreement's hall/row/slot position rather than appending a
    new one. Creating unconditionally meant the card could be filled in
    exactly once: correcting the planned exit date on the same slot was
    refused as a conflict with the row the agreement had just written, and
    choosing a different slot instead listed one asset in two places at
    the same time. ``place_asset`` already upserts, but only over the
    coordinate rows (``zone__isnull=False``), so it never reconciled these.
    """
    position = agreement.positions.filter(zone__isnull=True).first()
    validate_placement(
        hall,
        row,
        slot,
        planned_exit_date,
        exclude_pk=position.pk if position is not None else None,
    )
    if position is None:
        position = StoragePosition(agreement=agreement)
    position.hall = hall
    position.row = row
    position.slot = str(slot)
    position.planned_exit_date = planned_exit_date
    position.save()
    return position


def unplace_asset(agreement: StorageAgreement) -> bool:
    """WNT-05 "lift out": remove the agreement's coordinate placement so
    the ground goes back to free and the asset returns to the waiting
    list. Returns whether anything was removed.

    Only the coordinate placement is dropped; a legacy hall/row/slot
    record for the same agreement (WNT-06) is a different placement and
    is left alone.
    """
    position = agreement.positions.filter(zone__isnull=False).first()
    if position is None:
        return False
    position.delete()
    return True


# --- WNT-05: graphic yard layout (free 2D coordinate placement) ----------

# An asset with no recorded breadth is drawn at length/ratio, floored so it
# stays visible; keeps the footprint plausible without inventing data.
_DEFAULT_BEAM_RATIO = Decimal("3")
_MIN_BEAM_M = Decimal("1.5")


def asset_footprint_size(agreement: StorageAgreement) -> tuple:
    """The (length, width) in metres used to draw and place an asset on
    the yard. Width falls back to a fraction of the length (floored) when
    the breadth was not recorded, so the asset still renders to scale.

    Raises ``ValidationError`` when the length is unknown - it is the one
    dimension we cannot invent.
    """
    length = agreement.asset_length_m
    if length is None:
        raise ValidationError(
            _("Record the asset length before placing it on the yard (WNT-05).")
        )
    width = agreement.asset_width_m
    if width is None or width <= 0:
        # Decimal division keeps full precision (11.50 / 3 =
        # 3.833333...), which leaks into the bounds/collision error
        # messages staff read. The fallback is an estimate anyway, so
        # quantize it to centimetres - the precision the recorded
        # ``asset_width_m`` column itself carries (2 dp). A recorded
        # breadth is returned untouched.
        width = max(
            (length / _DEFAULT_BEAM_RATIO).quantize(Decimal("0.01")),
            _MIN_BEAM_M,
        )
    return length, width


def zone_footprint(length_m, width_m, x, y, orientation) -> tuple:
    """The axis-aligned footprint ``(x0, y0, x1, y1)`` in metres for a
    asset of ``length_m`` x ``width_m`` whose lower-left corner sits at
    ``(x, y)``. Crosswise turns the outline broadside, so the length runs
    along the exit edge (X) instead of the depth axis (Y)."""
    if orientation == StorageOrientation.CROSSWISE:
        dx, dy = length_m, width_m
    else:
        dx, dy = width_m, length_m
    return (x, y, x + dx, y + dy)


def _position_footprint(position: StoragePosition) -> tuple | None:
    """The footprint of an already-placed asset, or ``None`` when it
    cannot be computed because its length was never recorded.

    ``asset_footprint_size`` refuses to guess a length (WNT-05), and an
    already-placed neighbour can lose its length at any time (staff clear
    the field, or a placement predates the length being required). Such a
    neighbour has no geometry, so callers cannot test it - they get
    ``None`` rather than an exception about an asset the user is not
    touching. The graphic yard already renders this way (the layout view
    skips positions whose footprint is unknown), so validation and
    drawing agree on which assets exist on the plan.
    """
    try:
        length, width = asset_footprint_size(position.agreement)
    except ValidationError:
        return None
    return zone_footprint(
        length, width, position.pos_x_m, position.pos_y_m, position.orientation
    )


def _rectangles_overlap(a: tuple, b: tuple) -> bool:
    """True when two ``(x0, y0, x1, y1)`` rectangles share area (touching
    edges do not count as overlapping)."""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def inflate(footprint: tuple, margin) -> tuple:
    """Grow a footprint by ``margin`` on every side.

    Only the *candidate* is inflated when testing clearance, never both
    rectangles: inflating each would demand twice the configured gap.
    Growing one by the full margin makes the free space between the two
    outlines exactly ``margin``, which is the number staff set on the zone.
    """
    if not margin:
        return footprint
    x0, y0, x1, y1 = footprint
    return (x0 - margin, y0 - margin, x1 + margin, y1 + margin)


# --- WNT-09: the outline's real shape ---------------------------------------

def _fmt(value) -> str:
    """A metre value as a bare SVG number: two decimals, a dot, never a
    locale. Path data and polygon points are machine-read attributes, so
    they are formatted here rather than through a template filter."""
    return "%.2f" % float(value)


def outline_path(x0, y0, x1, y1, orientation) -> str:
    """SVG path data for a generic outline drawn inside the footprint
    rectangle ``(x0, y0, x1, y1)``: a pointed bow, the breadth widest just
    aft of midships, and a rounded transom.

    The bow faces the exit for a lengthwise asset (towards y = 0) and the
    left edge (x = 0) for a crosswise one - an asset on the hard is craned
    off its rack the way it went on, so the plan showing which way it
    points is not decoration. The rectangle itself stays the geometry
    every rule judges; the outline is how the plan *reads*.

    Coordinates are emitted with spaces only, no commas, so the ``d``
    attribute survives the same scrutiny as every other SVG number on the
    page (``tests_storage_svg_l10n``).
    """
    x0, y0, x1, y1 = (float(v) for v in (x0, y0, x1, y1))
    if orientation == StorageOrientation.CROSSWISE:
        length, breadth = x1 - x0, y1 - y0
    else:
        length, breadth = y1 - y0, x1 - x0
    half = breadth / 2.0

    # (u, v): u runs from the bow (0) to the stern (length), v across the
    # breadth, from -half to +half. Cubic curves to the widest point, a
    # straight run of topsides, then a quadratic corner into the transom.
    def uv(u, v):
        if orientation == StorageOrientation.CROSSWISE:
            return (x0 + u, y0 + half + v)
        return (x0 + half + v, y0 + u)

    def pt(u, v):
        x, y = uv(u, v)
        return "%s %s" % (_fmt(x), _fmt(y))

    L = length
    return " ".join(
        [
            "M", pt(0, 0),
            "C", pt(0.12 * L, -0.62 * half), pt(0.36 * L, -half), pt(0.58 * L, -half),
            "L", pt(0.92 * L, -half),
            "Q", pt(L, -half), pt(L, -0.55 * half),
            "L", pt(L, 0.55 * half),
            "Q", pt(L, half), pt(0.92 * L, half),
            "L", pt(0.58 * L, half),
            "C", pt(0.36 * L, half), pt(0.12 * L, 0.62 * half), pt(0, 0),
            "Z",
        ]
    )


def zone_conflicts(zone: StorageZone) -> list:
    """Assets already on the plan that the zone's *current* shape no
    longer accommodates: outside the box, or on ground an obstacle now
    reserves.

    Shrinking the yard (WNT-09) can pull the edge in under a parked asset.
    Nothing is moved automatically - the asset is physically where it is -
    but the editor reports every such case so the planner re-places them
    instead of finding out in October. Returns ``[(position, reason), ...]``.
    """
    conflicts = []
    obstacles = list(zone.obstacles.all())
    positions = zone.positions.filter(
        pos_x_m__isnull=False, pos_y_m__isnull=False
    ).select_related("agreement__case")
    for position in positions:
        footprint = _position_footprint(position)
        if footprint is None:
            continue
        x0, y0, x1, y1 = footprint
        if x0 < 0 or y0 < 0 or x1 > zone.width_m or y1 > zone.length_m:
            conflicts.append((position, _("runs past the yard edge")))
            continue
        for obstacle in obstacles:
            if _rectangles_overlap(footprint, obstacle.footprint):
                conflicts.append(
                    (position, _("sits on %(name)s") % {"name": obstacle.name})
                )
                break
    return conflicts


def validate_free_placement(
    zone: StorageZone,
    footprint: tuple,
    planned_exit_date,
    exclude_pk=None,
) -> None:
    """
    WNT-05: validate a coordinate placement drawn on the graphic yard.

    Five rules, each raising ``ValidationError`` naming the problem:

    * **Bounds** - the footprint must lie fully inside the zone rectangle.
    * **Obstacles** - it must not sit on ground reserved by a
      ``StorageObstacle`` (a lane, a crane track, a shed). Obstacles are
      tested against the bare footprint, without clearance: a lane exists
      to be driven, and a outline parked flush along its edge is exactly
      right.
    * **Collision** - it must not overlap another asset already placed
      in the zone (assets cannot share the same ground).
    * **Clearance** - and it must keep ``zone.clearance_m`` of working
      space from every other outline, so a stand, a ladder and a pressure
      washer still fit between them. Only the candidate is inflated, so
      the resulting gap is exactly the configured one.
    * **Exit order** - the exit is the front edge (y = 0); an asset that
      leaves earlier must keep a clear lane out. Within an overlapping
      X-span, no later-leaving asset may sit between it and the exit.
      This is the 2D generalisation of the hall/row/slot rule
      (``validate_placement``, WNT-06). Measured on the bare footprints:
      the clearance halo is working space, not a claim on the lane.
    """
    x0, y0, x1, y1 = footprint
    if x0 < 0 or y0 < 0 or x1 > zone.width_m or y1 > zone.length_m:
        raise ValidationError(
            _(
                "Asset does not fit in zone %(zone)s: its footprint "
                "[%(x0)s-%(x1)s] x [%(y0)s-%(y1)s] m falls outside the "
                "%(w)s x %(l)s m area."
            ) % {
                "zone": zone,
                "x0": x0,
                "x1": x1,
                "y0": y0,
                "y1": y1,
                "w": zone.width_m,
                "l": zone.length_m,
            }
        )

    for obstacle in zone.obstacles.all():
        if _rectangles_overlap(footprint, obstacle.footprint):
            raise ValidationError(
                _(
                    "Asset sits on %(name)s (%(kind)s) in zone %(zone)s - "
                    "that ground is permanently reserved."
                ) % {
                    "name": obstacle.name,
                    "kind": obstacle.get_kind_display(),
                    "zone": zone,
                }
            )

    clearance = zone.clearance_m or 0
    padded = inflate(footprint, clearance)

    new_exit = _exit_key(planned_exit_date)
    others = StoragePosition.objects.filter(
        zone=zone, pos_x_m__isnull=False, pos_y_m__isnull=False
    ).select_related("agreement")
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)

    for position in others:
        other = _position_footprint(position)
        if other is None:
            # A neighbour whose length was never recorded has no
            # footprint, so it can be neither collision- nor exit-tested.
            # It is skipped rather than allowed to veto this placement:
            # an unmeasured asset must never block placing a *different*
            # asset with an error naming a case the user is not working
            # on. The cost is that no collision is checked against it -
            # acceptable because it is equally absent from the graphic
            # yard, and recording its length restores both at once.
            continue
        if _rectangles_overlap(footprint, other):
            raise ValidationError(
                _(
                    "Asset overlaps position #%(id)s (agreement #%(agreement)s) "
                    "already placed in zone %(zone)s."
                ) % {
                    "id": position.pk,
                    "agreement": position.agreement_id,
                    "zone": zone,
                }
            )
        if _rectangles_overlap(padded, other):
            raise ValidationError(
                _(
                    "Asset is too close to position #%(id)s (agreement "
                    "#%(agreement)s): zone %(zone)s keeps %(gap)s m of "
                    "working space between outlines."
                ) % {
                    "id": position.pk,
                    "agreement": position.agreement_id,
                    "zone": zone,
                    "gap": clearance,
                }
            )
        ox0, oy0, ox1, oy1 = other
        x_overlap = not (x1 <= ox0 or ox1 <= x0)
        if not x_overlap:
            continue
        other_exit = _exit_key(position.planned_exit_date)
        # The other sits in front (nearer the exit) and leaves later -> it
        # would trap the earlier-leaving new asset behind it.
        if oy0 < y0 and other_exit > new_exit:
            raise ValidationError(
                _(
                    "WNT-05: cannot place the asset behind position #%(id)s in "
                    "zone %(zone)s - that asset is nearer the exit but leaves "
                    "later (%(other_exit)s), so it would block this one "
                    "(planned exit %(exit)s)."
                ) % {
                    "id": position.pk,
                    "zone": zone,
                    "other_exit": position.planned_exit_date
                    or _("none - leaves last"),
                    "exit": planned_exit_date or _("none - leaves last"),
                }
            )
        # The new asset would sit in front of a deeper one that leaves
        # earlier -> the new asset would block it.
        if y0 < oy0 and new_exit > other_exit:
            raise ValidationError(
                _(
                    "WNT-05: cannot place the asset in front of position "
                    "#%(id)s in zone %(zone)s - that asset is parked deeper "
                    "but leaves earlier (%(other_exit)s), so this one "
                    "(planned exit %(exit)s) would block it."
                ) % {
                    "id": position.pk,
                    "zone": zone,
                    "other_exit": position.planned_exit_date,
                    "exit": planned_exit_date or _("none - leaves last"),
                }
            )


def place_asset(
    agreement: StorageAgreement,
    zone: StorageZone,
    x,
    y,
    orientation: str = StorageOrientation.LENGTHWISE,
    planned_exit_date: datetime.date | None = None,
) -> StoragePosition:
    """
    WNT-05: place (or move) the agreement's asset on the graphic yard at
    metre coordinates ``(x, y)``. Validates bounds, collisions and exit
    order, then upserts the agreement's single coordinate placement.

    Raises ``ValidationError`` (naming the problem) when the placement is
    invalid; otherwise returns the saved ``StoragePosition``.

    Serialised on the zone row: two planners placing on the same free
    square within a second both passed the collision check.
    """
    with transaction.atomic():
        lock_row(zone)
        return _place_asset_locked(
            agreement, zone, x, y, orientation, planned_exit_date
        )


def _place_asset_locked(agreement, zone, x, y, orientation, planned_exit_date):
    length, width = asset_footprint_size(agreement)
    footprint = zone_footprint(length, width, x, y, orientation)

    position = agreement.positions.filter(zone__isnull=False).first()
    validate_free_placement(
        zone,
        footprint,
        planned_exit_date,
        exclude_pk=position.pk if position is not None else None,
    )

    if position is None:
        position = StoragePosition(agreement=agreement)
    position.zone = zone
    position.pos_x_m = x
    position.pos_y_m = y
    position.orientation = orientation
    position.planned_exit_date = planned_exit_date
    position.save()
    return position


# ---------------------------------------------------------------------------
# Expiry reminders (WNT-03 renewal lead time)
# ---------------------------------------------------------------------------

#: Default lead time, in days, for the storage-expiry reminder.
EXPIRY_REMINDER_DAYS = 30


def agreements_due_for_expiry_reminder(
    days: int = EXPIRY_REMINDER_DAYS, today: datetime.date | None = None
):
    """
    WNT-03/WNT-04: agreements whose commercial period ends within ``days``
    and whose client has not been reminded yet.

    The reminder exists to create renewal lead time: a package user who
    hears from us a month before 15 October can be sent a renewal offer
    while the spot is still theirs. Excluded are agreements with no
    ``period_end`` (nothing to remind about), periods that have already
    ended (too late - staff handle those by hand), cases already closed
    (``status.is_terminal``), and agreements already stamped with
    ``expiry_reminder_sent_at``.

    Ordered by ``period_end`` so the most urgent come first.

    "Today" is ``timezone.localdate()`` (today in ``TIME_ZONE``), not the
    server's ``date.today()``: a cron run just after midnight on a
    UTC-hosted server would otherwise use yesterday's date and reminders
    would land a day late at the edge of the window.
    """
    if today is None:
        today = timezone.localdate()
    return (
        StorageAgreement.objects.filter(
            period_end__isnull=False,
            period_end__gte=today,
            period_end__lte=today + datetime.timedelta(days=days),
            expiry_reminder_sent_at__isnull=True,
            case__status__is_terminal=False,
        )
        .select_related("case", "case__client", "case__status", "package")
        .order_by("period_end", "pk")
    )


def _expiry_reminder_message(agreement: StorageAgreement) -> tuple:
    """The (subject, body) of one expiry reminder, in the client's language.

    Pinned to ``settings.LANGUAGE_CODE`` by the caller like every other
    client mail (NTF-03) - a cron run has no UI language of its own.

    Dates use ``%d.%m.%Y.`` - the trailing dot of the Serbian convention
    this project writes client-facing dates in (as the WNT-03 quote text
    in ``create_storage_offer`` does).
    """
    subject = _("Your storage period ends on %(date)s") % {
        "date": agreement.period_end.strftime("%d.%m.%Y.")
    }
    lines = [
        _("Hello,"),
        "",
        _(
            "your asset's storage period ends on %(date)s. If you "
            "would like to extend it, reply to this e-mail or call us and we "
            "will prepare a renewal offer for you."
        ) % {"date": agreement.period_end.strftime("%d.%m.%Y.")},
    ]
    if agreement.package is not None:
        lines += [
            "",
            _("Current arrangement: %(package)s.") % {"package": agreement.package.name},
        ]
    lines += ["", _("Thank you.")]
    return subject, "\n".join(lines)


def send_expiry_reminders(
    days: int = EXPIRY_REMINDER_DAYS,
    today: datetime.date | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Send the storage-expiry reminder to every client due one, and stamp
    ``expiry_reminder_sent_at`` so a daily cron run never repeats itself.

    A client with no e-mail address on file is not skipped silently: a
    SYSTEM ``ActivityLog`` entry asks staff to phone them instead (the
    ``notify_case_created`` convention), and the agreement is stamped all
    the same, so the reminder surfaces once and does not pile up.

    One failing agreement must not abort the run - every send is
    independent, and a send failure leaves the stamp unset so the next
    run retries it. Returns counts: ``{"emailed", "to_phone", "failed"}``.
    With ``dry_run`` nothing is sent, logged or stamped; the counts then
    report what *would* happen (everything with an address counts as
    ``emailed``).
    """
    from django.apps import apps as django_apps
    from django.conf import settings
    from django.utils import translation

    from apps.notifications.services import client_email_for, send_client_email

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    counts = {"emailed": 0, "to_phone": 0, "failed": 0}

    for agreement in agreements_due_for_expiry_reminder(days=days, today=today):
        case = agreement.case
        has_address = bool(client_email_for(case))
        if dry_run:
            counts["emailed" if has_address else "to_phone"] += 1
            continue

        with translation.override(settings.LANGUAGE_CODE):
            if has_address:
                subject, body = _expiry_reminder_message(agreement)
                sent = send_client_email(case, subject, body)
                if not sent:
                    counts["failed"] += 1
                    continue
                counts["emailed"] += 1
                text = (
                    "WNT-03: storage expiry reminder e-mailed to the client "
                    "(period ends %s)." % agreement.period_end
                )
            else:
                counts["to_phone"] += 1
                text = (
                    "WNT-03: storage period ends %s and no client e-mail is on "
                    "file - remind the client by phone and offer a renewal."
                    % agreement.period_end
                )

        ActivityLog.objects.create(
            case=case, author=None, kind=ActivityLog.Kind.SYSTEM, text=text
        )
        agreement.expiry_reminder_sent_at = timezone.now()
        agreement.save(update_fields=["expiry_reminder_sent_at"])

    return counts

