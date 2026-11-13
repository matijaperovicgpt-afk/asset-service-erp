"""Business logic for the quotes app.

Implemented:
- required_approval_levels(amount): which approval levels a quote of a
  given amount needs, per the ``ApprovalLimit`` configuration (ACT-02,
  SVC-12, INV-04).
- missing_approval_levels(quote): required levels not yet approved on
  the quote (used by the ``awaiting_approval -> in_progress`` guard,
  SVC-13).
- send_quote(quote, user): mark a DRAFT quote as SENT (SVC-12). No
  e-mail is sent here: the offer itself reaches the client through the
  channel staff choose (phone, e-mail, SMS), and the client's answer is
  taken by staff and recorded with ``record_approval`` (ACT-03). There
  is no online decision page.
- record_approval(quote, level, decision, ...): record a decision on the
  internal approval ladder or client consent (ACT-02, INV-04) and derive
  the quote status (ACCEPTED/REJECTED). Refuses decisions on settled
  quotes, at levels the amount does not require, and repeat decisions at
  an already-approved level.
- request_advance_payment(case, quote, percent, ...): create an advance
  payment request (50% default for non-stock parts and
  transport).
- create_quote(case, description, amount, ...): draft a quote on the
  case (SVC-11/SVC-12); non-positive amounts refused.
- mark_advance_paid(advance, user): record the actual payment -
  ``paid_at`` set once (TRP-04, INV-05/INV-06).
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.models import ApprovalLimit, Role
from apps.cases.locking import serialised_per_row

from .models import AdvancePayment, Approval, Quote

logger = logging.getLogger(__name__)


def required_approval_levels(amount: Decimal) -> list:
    """
    Return the ``Approval.Level`` values a quote of ``amount`` requires.

    Client consent (CLIENT) is always required. The internal ladder
    (ACT-02) uses ``ApprovalLimit`` rows: amounts within the technician
    limit proceed on the service's own authority (no internal approval
    row), amounts within the manager limit need a MANAGER approval, and
    anything above needs a DIRECTOR approval. Missing limit rows are
    treated conservatively (escalate to the next level up).
    """
    levels = [Approval.Level.CLIENT]

    limits = {row.role: row.max_amount for row in ApprovalLimit.objects.all()}
    technician_limit = limits.get(Role.TECHNICIAN)
    if technician_limit is not None and amount <= technician_limit:
        return levels

    manager_limit = limits.get(Role.MANAGER)
    if manager_limit is not None and amount <= manager_limit:
        levels.append(Approval.Level.MANAGER)
    else:
        levels.append(Approval.Level.DIRECTOR)
    return levels


def missing_approval_levels(quote: Quote) -> list:
    """
    Return the required approval levels that have no APPROVED decision
    recorded on ``quote`` (INV-04). Empty list means fully approved.
    """
    approved = set(
        quote.approvals.filter(decision=Approval.Decision.APPROVED).values_list(
            "level", flat=True
        )
    )
    return [
        level for level in required_approval_levels(quote.amount)
        if level not in approved
    ]


def log_price_list_basis(quote: Quote, user, items, suggested_total) -> None:
    """
    ACT-02 audit trail (the ``record_price_deviation`` convention): which
    price-list ``items`` suggested the amount of ``quote``, what they
    added up to (``suggested_total``, summed by the form so the log can
    never contradict the quote) and what staff actually quoted. Plain
    strings - stored data. A quote drafted without catalogue items has
    no basis to log.
    """
    if not items:
        return
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=quote.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "Quote #%s drafted from price-list items: %s "
            "(suggested total %s EUR; final amount %s EUR)."
            % (
                quote.pk,
                ", ".join(item.name for item in items),
                suggested_total,
                quote.amount,
            )
        ),
    )


def latest_quote(case):
    """The current revision of ``case``'s offer: the most recently created
    ``Quote`` (pk breaks a tie), or ``None``. Both the ``in_progress``
    guard and the ladder-change promotion judge this one row, so the
    ordering lives here once."""
    return (
        Quote.objects.filter(case=case).order_by("-created_at", "-pk").first()
    )


def accept_fully_approved_quotes(user=None) -> list:
    """
    After the approval ladder changes (OPEN-01), a SENT quote whose
    required levels are all approved already is accepted here - no
    further decision may be recorded on it (INV-04), so nothing else
    would ever promote it and its case would wait for approval forever.
    Returns the quotes promoted.

    Only the live quote of an open case is considered: a superseded
    revision that was still SENT when the client approved it must not
    quietly become the accepted offer because a limit moved. Each
    promotion happens under the quote's row lock (the same lock
    ``record_approval`` holds for this recomputation) and is written to
    the case log naming the ladder change and who made it (INV-03).
    """
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    candidates = (
        Quote.objects.filter(
            status=Quote.Status.SENT,
            case__status__is_terminal=False,
            approvals__level=Approval.Level.CLIENT,
            approvals__decision=Approval.Decision.APPROVED,
        )
        .distinct()
        .values_list("pk", flat=True)
    )
    promoted = []
    for pk in list(candidates):
        with transaction.atomic():
            quote = Quote.objects.select_for_update().get(pk=pk)
            latest = latest_quote(quote.case_id)
            if (
                latest is None
                or latest.pk != quote.pk
                or quote.status != Quote.Status.SENT
                or missing_approval_levels(quote)
            ):
                continue
            quote.status = Quote.Status.ACCEPTED
            quote.save(update_fields=["status"])
            ActivityLog.objects.create(
                case=quote.case,
                author=user,
                kind=ActivityLog.Kind.SYSTEM,
                text=(
                    "Quote #%s accepted: every approval level it needs under "
                    "the changed approval limits is already approved (INV-04)."
                    % quote.pk
                ),
            )
            promoted.append(quote)
    return promoted


@serialised_per_row
def send_quote(quote: Quote, user) -> Quote:
    """
    Move ``quote`` from DRAFT to SENT (SVC-12).

    Refuses quotes that are not in DRAFT or have a non-positive amount.
    Does not e-mail the client and does not touch the case status: the
    offer (amount, scope, validity) reaches the client through whichever
    channel staff use, and there is no page where the client could read
    or answer it. Writes a SYSTEM ActivityLog entry on the case instead.
    """
    if quote.status != Quote.Status.DRAFT:
        raise ValidationError(
            _("Quote #%(id)s is %(status)s; only DRAFT quotes can be sent.")
            % {"id": quote.pk, "status": quote.status}
        )
    if quote.amount is None or quote.amount <= 0:
        raise ValidationError(
            _(
                "Quote #%(id)s has amount %(amount)s; a positive amount is "
                "required to send."
            ) % {"id": quote.pk, "amount": quote.amount}
        )
    quote.status = Quote.Status.SENT
    quote.save(update_fields=["status"])

    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=quote.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text="Quote #%s sent, amount %s %s"
        % (quote.pk, quote.amount, quote.currency),
    )
    return quote


#: Which staff roles may decide each internal approval level (ACT-02).
_INTERNAL_LEVEL_ROLES = {
    Approval.Level.MANAGER: (Role.MANAGER, Role.DIRECTOR),
    Approval.Level.DIRECTOR: (Role.DIRECTOR,),
}


def record_approval(
    quote: Quote, level: str, decision: str, user=None, note: str = ""
) -> Approval:
    """
    Record an approval ``decision`` at ``level`` on ``quote`` (ACT-02,
    INV-04) and derive the quote status.

    Internal levels (manager/director) require ``user`` to have the
    matching role authority (superusers bypass). CLIENT-level decisions
    keep ``decided_by`` empty - clients have no accounts (ACT-03) - and
    ``user`` is the staff member recording it, named in the note.

    The quote becomes ACCEPTED once it has an APPROVED CLIENT decision
    and no missing approval levels; a REJECTED CLIENT decision makes it
    REJECTED.

    Guards (INV-04): a settled (ACCEPTED/REJECTED) quote takes no
    further decisions, a level the amount does not require is refused
    (e.g. MANAGER on a director-level amount), and a level with an
    APPROVED decision cannot be decided again.

    Every writer of a quote's approval set goes through here, so this is
    where they serialise: the quote row is locked for the whole function
    and the "is everything approved now?" recomputation happens under
    that lock. Without it two approvals racing from different channels
    (a client accepting online while the director approves in the
    backoffice) can each miss the other's row and leave the quote SENT
    with every level approved - a state no code path can then repair,
    since neither level may be decided twice.
    """
    with transaction.atomic():
        return _record_approval_locked(quote, level, decision, user, note)


def _record_approval_locked(quote, level, decision, user, note):
    """``record_approval`` body, running inside the caller's transaction
    with the quote row locked."""
    locked = Quote.objects.select_for_update().get(pk=quote.pk)
    if locked.status in (Quote.Status.ACCEPTED, Quote.Status.REJECTED):
        raise ValidationError(
            _(
                "Quote #%(id)s is already %(status)s; no further approval "
                "decisions can be recorded (INV-04)."
            ) % {"id": locked.pk, "status": locked.get_status_display()}
        )
    # SVC-12 comes before SVC-13: nobody can consent to, or sign off, an
    # offer that has not gone out. Recording a client approval on a DRAFT
    # used to accept it and skip the send entirely (QA SV-H1).
    if locked.status == Quote.Status.DRAFT:
        raise ValidationError(
            _(
                "Quote #%(id)s has not been sent yet; send it before "
                "recording any approval decision (SVC-12)."
            ) % {"id": locked.pk}
        )
    if level in _INTERNAL_LEVEL_ROLES:
        allowed_roles = _INTERNAL_LEVEL_ROLES[level]
        if user is None or not (
            user.is_superuser or user.role in allowed_roles
        ):
            raise ValidationError(
                _("A %(level)s-level decision requires role %(roles)s (got %(got)s).")
                % {
                    "level": level,
                    "roles": _(" or ").join(allowed_roles),
                    "got": getattr(user, "role", None) or _("no user"),
                }
            )
        decided_by = user
    elif level == Approval.Level.CLIENT:
        decided_by = None
        if user is not None:
            recorded_by = "Recorded by %s." % (
                user.get_full_name() or user.get_username()
            )
            note = "%s %s" % (note, recorded_by) if note else recorded_by
    else:
        raise ValidationError(
            _("Unknown approval level: %(level)s.") % {"level": level}
        )

    required = required_approval_levels(locked.amount)
    if level not in required:
        raise ValidationError(
            _(
                "Quote #%(id)s (%(amount)s %(currency)s) does not require a "
                "%(level)s-level decision; it requires: %(required)s (ACT-02)."
            ) % {
                "id": locked.pk,
                "amount": locked.amount,
                "currency": locked.currency,
                "level": level,
                "required": ", ".join(required),
            }
        )
    if locked.approvals.filter(
        level=level, decision=Approval.Decision.APPROVED
    ).exists():
        raise ValidationError(
            _(
                "Quote #%(id)s already has an approved %(level)s-level "
                "decision; it cannot be recorded again (INV-04)."
            ) % {"id": locked.pk, "level": level}
        )

    approval = Approval.objects.create(
        quote=locked,
        level=level,
        decision=decision,
        decided_by=decided_by,
        decided_at=timezone.now(),
        note=note,
    )

    if (
        level == Approval.Level.CLIENT
        and decision == Approval.Decision.REJECTED
    ):
        locked.status = Quote.Status.REJECTED
        locked.save(update_fields=["status"])
    else:
        client_approved = locked.approvals.filter(
            level=Approval.Level.CLIENT,
            decision=Approval.Decision.APPROVED,
        ).exists()
        if client_approved and not missing_approval_levels(locked):
            locked.status = Quote.Status.ACCEPTED
            locked.save(update_fields=["status"])
    # The caller holds its own instance; keep the status it can see in
    # step with the row that was just written.
    quote.status = locked.status
    return approval


def request_advance_payment(
    case, quote: Quote = None, percent: int = 50, due_date=None, amount=None
) -> AdvancePayment:
    """
    Create an ``AdvancePayment`` on ``case``.

    With a ``quote``, the amount is ``percent`` of the quote amount,
    quantized to 2 decimals; otherwise ``amount`` must be given
    explicitly. Enforcing percent >= 50 for non-stock parts is the
    caller's concern (``parts.services.order_part`` enforces INV-05).
    """
    if quote is not None:
        amount = (
            Decimal(percent) / Decimal(100) * quote.amount
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    elif amount is None:
        raise ValidationError(
            _("An explicit amount is required when no quote is given.")
        )
    return AdvancePayment.objects.create(
        case=case,
        quote=quote,
        percent=percent,
        amount=amount,
        due_date=due_date,
    )


def create_quote(
    case,
    description: str,
    amount,
    user=None,
    currency: str = "EUR",
    valid_until=None,
) -> Quote:
    """
    SVC-11/SVC-12: draft a quote on the case. The same
    amount guard ``send_quote()`` applies at send time is enforced at
    creation so a worthless draft never exists.
    """
    if amount is None or amount <= 0:
        raise ValidationError(
            _("A quote needs a positive amount (got %(amount)s).")
            % {"amount": amount}
        )
    quote = Quote.objects.create(
        case=case,
        description=description,
        amount=amount,
        currency=currency,
        valid_until=valid_until,
    )
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-12: quote #%s created (%s %s)."
            % (quote.pk, quote.amount, quote.currency)
        ),
    )
    return quote


@serialised_per_row
def mark_advance_paid(advance: AdvancePayment, user) -> AdvancePayment:
    """
    TRP-04 / INV-05: record that an advance payment was
    actually paid - ``paid_at`` set once, double payment refused. A paid
    advance is what the ``departure_scheduled`` guard (INV-06) and
    non-stock part orders rely on.
    """
    # Money received is recorded by the office, as settlement is - not
    # by the workshop (QA SV-M8 / R-L2).
    if user is not None and not user.is_superuser and user.role == Role.TECHNICIAN:
        raise ValidationError(
            _("A technician cannot record a payment; the office does (TRP-04).")
        )
    if advance.paid_at is not None:
        raise ValidationError(
            _("Advance payment #%(id)s was already marked paid at %(when)s.")
            % {"id": advance.pk, "when": advance.paid_at}
        )
    advance.paid_at = timezone.now()
    advance.save(update_fields=["paid_at"])
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=advance.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "TRP-04/INV-05: advance payment #%s (%s%%, %s) marked paid."
            % (advance.pk, advance.percent, advance.amount)
        ),
    )
    return advance
