"""Domain services for the repair flow.

Covers the work-order lifecycle (open, finalize, sign),
the diagnostic record (SVC-09), the execution log (SVC-15: work log
entries, used parts), the quality-control test run (SVC-16) and the
client completion notice (SVC-17).
"""
from decimal import ROUND_HALF_UP, Decimal

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.cases.locking import serialised_per_row
from .models import (
    DiagnosticPart,
    ServicePriceItem,
    CompletionNotice,
    Diagnostic,
    TestRun,
    UsedPart,
    WorkLogEntry,
    WorkOrder,
    InspectionCheckItem,
    InspectionPoint,
    InspectionRecord,
)


def _log_system(case, user, text: str) -> None:
    ActivityLog = django_apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case, author=user, kind=ActivityLog.Kind.SYSTEM, text=text
    )


@serialised_per_row
def create_work_order(case, user, is_preliminary: bool = True) -> WorkOrder:
    """
    SVC-02: a work order is opened for the service case; the preliminary
    order is what the client signs at intake (SVC-05). ``WorkOrder.case``
    is one-to-one - a second order on the same case is refused.
    """
    existing = WorkOrder.objects.filter(case=case).first()
    if existing is not None:
        raise ValidationError(
            _("Case #%(id)s already has work order #%(order)s.")
            % {"id": case.pk, "order": existing.pk}
        )
    work_order = WorkOrder.objects.create(case=case, is_preliminary=is_preliminary)
    _log_system(
        case,
        user,
        "SVC-02: %s work order #%s opened."
        % ("preliminary" if is_preliminary else "final", work_order.pk),
    )
    return work_order


def ensure_preliminary_work_order(case, user) -> WorkOrder | None:
    """
    SVC-02 (procedure §1.2 "Initial registration"): once the client is
    known, the report is registered AND a preliminary work order is
    opened - the document the client later signs on arrival (SVC-05).

    Called when staff confirm the client match at triage, which is the
    moment a case first has a known client (GEN-03 keeps the raw report
    unattached until then). Idempotent and quiet: a case that already has
    an order, or that is not a SERVICE case, gets nothing and no error -
    a work order means nothing on a transport or storage case.
    """
    from apps.cases.models import CaseType

    if case.case_type != CaseType.SERVICE:
        return None
    existing = WorkOrder.objects.filter(case=case).first()
    if existing is not None:
        return existing
    work_order = WorkOrder.objects.create(case=case, is_preliminary=True)
    _log_system(
        case,
        user,
        "SVC-02: preliminary work order #%s opened for a known client."
        % work_order.pk,
    )
    return work_order


def finalize_work_order(work_order: WorkOrder, user) -> WorkOrder:
    """
    SVC-05: the employee checks the client's entries and endorses the
    order, marking it final (``is_preliminary=False``)
    before it goes to service. Who did that is recorded on the order
    itself, beside the client's scanned signature and the technician's
    in-app one - three acts, three records.

    A signature captured on the preliminary order is retained - the model
    keeps one work order per case, and the ``asset_received`` guard
    requires ``is_preliminary=False`` plus ``signed_at`` set.
    """
    if not work_order.is_preliminary:
        raise ValidationError(
            _("Work order #%(id)s is already final.") % {"id": work_order.pk}
        )
    if user is None:
        raise ValidationError(
            _("A staff user is required to endorse work order #%(id)s (SVC-05).")
            % {"id": work_order.pk}
        )
    work_order.is_preliminary = False
    work_order.verified_by = user
    work_order.verified_at = timezone.now()
    work_order.save(
        update_fields=["is_preliminary", "verified_by", "verified_at"]
    )
    _log_system(
        work_order.case,
        user,
        "SVC-05: work order #%s checked and endorsed by '%s' (marked FINAL)."
        % (work_order.pk, user.get_username()),
    )
    return work_order


def close_work_order(work_order: WorkOrder, user) -> WorkOrder:
    """
    SVC-20 (procedure §4.5 "Closing the order"): "The work order and the
    service ticket are closed only once completion, asset handover and
    financial status have been recorded."

    The three preconditions are not re-checked here, and deliberately so:
    they are exactly what ``_guard_closed`` already enforces on the case,
    and this runs *because* that guard passed. Duplicating them would put
    the same rule in two places, where the copies drift.

    Idempotent: a case that is reopened and closed again keeps the first
    closing stamp, which is the one that matches the handover.
    """
    if work_order.closed_at is not None:
        return work_order
    work_order.closed_by = user
    work_order.closed_at = timezone.now()
    work_order.save(update_fields=["closed_by", "closed_at"])
    _log_system(
        work_order.case,
        user,
        "SVC-20: work order #%s closed with the case." % work_order.pk,
    )
    return work_order


def reopen_work_order(work_order: WorkOrder, user, *, reason: str) -> WorkOrder:
    """
    SVC-20: undo a close so a correction can be recorded. The procedure
    closes the order at the end of the job, but it does not claim the
    document is infallible - a part billed to the wrong order still has
    to be fixable. What it should not be is silent, so this needs a
    stated reason and leaves both the close and the reopening in the log.
    """
    # The same two roles that own the closing transition and the
    # financial-settlement flag (SVC-19): undoing a close is a management
    # act, not a correction a technician makes on the way past.
    from apps.cases.services import SETTLEMENT_ROLES

    if user is None or (
        not user.is_superuser and user.role not in SETTLEMENT_ROLES
    ):
        raise ValidationError(
            _(
                "Role '%(role)s' may not reopen work order #%(id)s "
                "(allowed: %(allowed)s)."
            ) % {
                "role": (user.role if user else None) or _("(none)"),
                "id": work_order.pk,
                "allowed": ", ".join(SETTLEMENT_ROLES),
            }
        )
    if work_order.closed_at is None:
        raise ValidationError(
            _("Work order #%(id)s is not closed.") % {"id": work_order.pk}
        )
    if not (reason or "").strip():
        raise ValidationError(
            _("State why work order #%(id)s is being reopened (SVC-20).")
            % {"id": work_order.pk}
        )
    was_closed_at = work_order.closed_at
    work_order.closed_by = None
    work_order.closed_at = None
    work_order.save(update_fields=["closed_by", "closed_at"])
    _log_system(
        work_order.case,
        user,
        "SVC-20: work order #%s (closed %s) reopened by '%s'. Reason: %s"
        % (
            work_order.pk,
            timezone.localtime(was_closed_at).strftime("%d.%m.%Y %H:%M"),
            user.get_username() if user else "-",
            reason.strip(),
        ),
    )
    return work_order


def has_client_approval(work_order: WorkOrder) -> bool:
    """SVC-05: is the client-signed scan attached to the work order?"""
    from apps.attachments.services import attachments_for

    Attachment = django_apps.get_model("attachments", "Attachment")
    return attachments_for(work_order).filter(kind=Attachment.Kind.DOCUMENT).exists()


def attach_client_approval(work_order: WorkOrder, file, user):
    """
    SVC-05: the client's legal approval is the scanned, client-signed
    paper work order. Stored as a DOCUMENT attachment on the work order
    itself (shared upload rules apply, INV-08); re-uploads are allowed -
    the guard only requires that at least one scan exists.
    """
    from apps.attachments.services import attach_file

    Attachment = django_apps.get_model("attachments", "Attachment")
    attachment = attach_file(
        work_order,
        file,
        Attachment.Kind.DOCUMENT,
        Attachment.Stage.INTAKE,
        user=user,
    )
    _log_system(
        work_order.case,
        user,
        "SVC-05: client-approval scan '%s' attached to work order #%s."
        % (attachment.original_name, work_order.pk),
    )
    return attachment


def sign_work_order(work_order: WorkOrder, user) -> WorkOrder:
    """
    SVC-05: the technician responsible for the fix signs the work order -
    the company-side record of when the work item actually started.
    Requires the client-approval scan to be attached first (the client
    signs on paper before the order is forwarded to service), a
    TECHNICIAN role, and signs once only. If the case has no assigned
    technician yet, the signer becomes it.
    """
    from apps.accounts.models import Role

    if user is None or (not user.is_superuser and user.role != Role.TECHNICIAN):
        raise ValidationError(
            _(
                "Only a technician may sign work order #%(id)s - the "
                "signature records who is responsible for the fix (SVC-05)."
            ) % {"id": work_order.pk}
        )
    if not has_client_approval(work_order):
        raise ValidationError(
            _(
                "Work order #%(id)s has no client-approval scan attached; "
                "upload the client-signed document first (SVC-05)."
            ) % {"id": work_order.pk}
        )
    if work_order.signed_at is not None:
        raise ValidationError(
            _(
                "Work order #%(id)s was already signed at %(when)s and "
                "cannot be re-signed (SVC-05)."
            ) % {"id": work_order.pk, "when": work_order.signed_at}
        )
    work_order.signed_by_technician = user
    work_order.signed_at = timezone.now()
    work_order.save(update_fields=["signed_by_technician", "signed_at"])

    case = work_order.case
    if case.assigned_technician_id is None:
        case.assigned_technician = user
        case.save(update_fields=["assigned_technician"])

    _log_system(
        case,
        user,
        "SVC-05: work order #%s signed by technician '%s'."
        % (work_order.pk, user.get_username()),
    )
    return work_order


def record_diagnostic(
    work_order: WorkOrder,
    user,
    *,
    issue: str,
    servicing_work: str,
    estimated_parts_price,
    estimated_completion_date,
    parts_needed: str = "",
    needed_parts=None,
) -> Diagnostic:
    """
    SVC-09: record the diagnostic on the work order. All pieces are
    mandatory (the model enforces it; the diagnostics-exit guard only
    counts complete rows). Additional findings discovered later are
    recorded as further rows - existing rows are never edited.

    SVC-10: "parts needed" has two halves. ``needed_parts`` is an iterable
    of ``(part, quantity)`` for everything the catalogue knows, which is
    what the procedure's §3.2 shortcut guard reads; ``parts_needed`` is
    free text for what it does not. At least one of the two must say
    something - which half is a matter of how complete the catalogue is,
    not of how complete the diagnosis is.
    """
    needed_parts = list(needed_parts or [])
    if not (parts_needed or "").strip() and not needed_parts:
        raise ValidationError(
            _(
                "Record the parts the job needs: select them from the "
                "catalogue, or describe the ones it has no row for (SVC-10)."
            )
        )
    for part, quantity in needed_parts:
        if quantity is None or quantity < 1:
            raise ValidationError(
                _("Quantity for part '%(part)s' must be at least 1 (SVC-10).")
                % {"part": part}
            )

    diagnostic = Diagnostic.objects.create(
        work_order=work_order,
        issue=issue,
        servicing_work=servicing_work,
        parts_needed=(parts_needed or "").strip(),
        estimated_parts_price=estimated_parts_price,
        estimated_completion_date=estimated_completion_date,
    )
    for part, quantity in needed_parts:
        DiagnosticPart.objects.create(
            diagnostic=diagnostic, part=part, quantity=quantity
        )
    _log_system(
        work_order.case,
        user,
        "SVC-09: diagnostic #%s recorded on work order #%s "
        "(%s catalogue part(s)%s, estimated at %s EUR, completion %s)."
        % (
            diagnostic.pk,
            work_order.pk,
            len(needed_parts),
            " plus uncatalogued parts" if diagnostic.parts_needed else "",
            estimated_parts_price,
            estimated_completion_date,
        ),
    )
    return diagnostic

def is_new_client(case) -> bool:
    """
    SVC-08: is this case for a new client? A client is new if they have
    no finished case on record. A case with no linked client also counts
    as new.
    """

    if case.client_id is None:
        return True
    Case = django_apps.get_model("cases", "Case")
    return not (
        Case.objects.filter(client_id=case.client_id, status__is_terminal=True)
        .exclude(pk=case.pk)
        .exists()
    )

@serialised_per_row
def start_inspection(work_order: WorkOrder, user) -> InspectionRecord:
    """
    SVC-08: open the full new-client inspection and materialize one
    ``InspectionCheckItem`` per active ``InspectionPoint``. One inspection
    per work order; a second call is refused. The technician records a
    status per point and signs the inspection once every point is filled.
    """

    existing = InspectionRecord.objects.filter(
        work_order=work_order,
    ).first()
    if existing is not None:
        raise ValidationError(
            _("Work order #%(id)s already has inspection record #%(record)s.")
            % {"id": work_order.pk, "record": existing.pk}
        )
    record = InspectionRecord.objects.create(
        work_order=work_order,
        performed_by=user,
    )
    points = list(InspectionPoint.objects.filter(is_active=True))
    InspectionCheckItem.objects.bulk_create(
        InspectionCheckItem(
            record=record,
            point=point,
        ) for point in points
    )
    _log_system(
        work_order.case,
        user,
        "SVC-08: full new-client inspection #%s opened on work order #%s"
        " (%s inspection points)." % (record.pk, work_order.pk, len(points)),
    )
    return record

def record_inspection_result(
    item: InspectionCheckItem, user, *, status: str, note: str = ""
) -> InspectionCheckItem:
    """
    SVC-08: record the technician's finding on one checklist point
    (status + note). Refused once the record is signed - a signed
    inspection is a finished record.
    """
    if item.record.signed_at is not None:
        raise ValidationError(
            _(
                "Inspection record #%(id)s is signed and can no longer be "
                "edited (SVC-08)."
            ) % {"id": item.record_id}
        )
    if status not in InspectionCheckItem.Status.values:
        raise ValidationError(_("Choose a valid status for the point (SVC-08)."))
    item.status = status
    item.note = (note or "").strip()
    item.save(update_fields=["status", "note"])
    return item


def record_inspection_results(
    record: InspectionRecord, user, results: dict
) -> int:
    """
    SVC-08: record the technician's findings for many points of one
    inspection at once. ``results`` maps ``item_pk -> (status, note)``;
    every point named must belong to ``record``. Rows whose status and
    note are unchanged are skipped, the rest go through
    :func:`record_inspection_result` inside one transaction - so a bad
    status in row 30 leaves rows 1-29 untouched as well - and one activity
    line records how many points changed. Returns that count.
    """
    if record.signed_at is not None:
        raise ValidationError(
            _(
                "Inspection record #%(id)s is signed and can no longer be "
                "edited (SVC-08)."
            ) % {"id": record.pk}
        )
    items = {item.pk: item for item in record.items.select_related("record")}
    changed = 0
    with transaction.atomic():
        for item_pk, (status, note) in results.items():
            item = items.get(item_pk)
            if item is None:
                raise ValidationError(
                    _(
                        "Point #%(pk)s does not belong to inspection "
                        "#%(id)s (SVC-08)."
                    ) % {"pk": item_pk, "id": record.pk}
                )
            note = (note or "").strip()
            if item.status == status and item.note == note:
                continue
            record_inspection_result(item, user, status=status, note=note)
            changed += 1
        if changed:
            _log_system(
                record.work_order.case,
                user,
                "SVC-08: %s inspection point(s) recorded on inspection #%s."
                % (changed, record.pk),
            )
    return changed


def sign_inspection_record(record: InspectionRecord, user) -> InspectionRecord:
    """
    SVC-08: sign off the full inspection, once only. Every checklist point
    must have a status (no point left unanswered).
    The signature belongs to the technician responsible for the case,
    otherwise to the next responsible staff member - mirroring the intake
    record (SVC-06). A signed inspection is what a new client requires
    before the final estimate can be issued.
    """
    if user is None:
        raise ValidationError(
            _("A staff user is required to sign the inspection (SVC-08).")
        )
    if record.signed_at is not None:
        raise ValidationError(
            _(
                "Inspection record #%(id)s was already signed at %(when)s "
                "and cannot be re-signed (SVC-08)."
            ) % {"id": record.pk, "when": record.signed_at}
        )
    items = list(record.items.all())
    if not items:
        raise ValidationError(
            _("The inspection has no checklist points to sign (SVC-08).")
        )
    unresolved = sum(1 for item in items if not item.status)
    if unresolved:
        raise ValidationError(
            _(
                "Resolve all %(n)s remaining checklist point(s) before "
                "signing the inspection (SVC-08)."
            ) % {"n": unresolved}
        )
    signer = record.work_order.case.assigned_technician or user
    # The front desk may enter the signature on the assigned technician's
    # behalf (it is credited to them), but with nobody assigned the front
    # desk itself became the signer of an inspection it never did (QA
    # R-L2 / SV-M7).
    if not signer.is_superuser and signer.role == "FRONT_DESK":
        raise ValidationError(
            _(
                "Assign the technician who performed the inspection first - "
                "it is signed in their name, or by a manager (SVC-08)."
            )
        )
    record.signed_by = signer
    record.signed_at = timezone.now()
    record.save(update_fields=["signed_by", "signed_at"])
    _log_system(
        record.work_order.case,
        user,
        "SVC-08: full inspection #%s signed by '%s'."
        % (record.pk, signer.get_username() if signer else "?"),
    )
    return record

def inspection_complete_for_case(case) -> bool:
    """SVC-08: does the case's work order carry a signed inspection?
    (The new-client transition guard uses this.)"""
    return InspectionRecord.objects.filter(
        work_order__case=case, signed_at__isnull=False
    ).exists()

def _require_workable_order(work_order: WorkOrder) -> None:
    """
    The two states in which nothing may be written to a work order.

    SVC-15 (procedure §4.1): "The technician starts work **according
    to the approved order**." A preliminary order is one nobody has checked
    and endorsed yet (SVC-05, §2.1) - work recorded against it is work
    done against an order the company has not accepted. This became
    reachable across the whole lifecycle once SVC-02 started opening a
    preliminary order at triage, so the order is now the thing that has
    to be approved, not merely present.

    SVC-20 (procedure §4.5): a closed order takes no further entries.
    Corrections after closing are a deliberate act - reopening - not a
    silent append to a document that has been handed over and settled.
    """
    if work_order.closed_at is not None:
        raise ValidationError(
            _(
                "Work order #%(id)s was closed on %(when)s and takes no "
                "further entries (SVC-20)."
            ) % {
                "id": work_order.pk,
                "when": timezone.localtime(work_order.closed_at).strftime(
                    "%d.%m.%Y %H:%M"
                ),
            }
        )
    if work_order.is_preliminary:
        raise ValidationError(
            _(
                "Work order #%(id)s is still preliminary - the work is done "
                "against an endorsed order, so have it checked and endorsed "
                "first (SVC-15)."
            ) % {"id": work_order.pk}
        )


def log_work(work_order: WorkOrder, user, *, description: str, hours) -> WorkLogEntry:
    """
    SVC-15 (procedure §4.1 "Carrying out the work"): record one performed work
    item on the work order - what was done and the time spent, attributed
    to the acting staff member. Entries are append-only from the UI, and
    only against an endorsed, still-open order.
    """
    _require_workable_order(work_order)
    if not (description or "").strip():
        raise ValidationError(
            _("Describe the performed work (SVC-15).")
        )
    if hours is None or hours <= 0:
        raise ValidationError(
            _("Working time must be a positive number of hours (SVC-15).")
        )
    entry = WorkLogEntry.objects.create(
        work_order=work_order,
        description=description.strip(),
        hours=hours,
        performed_by=user,
    )
    _log_system(
        work_order.case,
        user,
        "SVC-15: work log entry #%s recorded on work order #%s (%s h)."
        % (entry.pk, work_order.pk, entry.hours),
    )
    return entry


def record_used_part(work_order: WorkOrder, user, *, part, quantity: int = 1) -> UsedPart:
    """
    SVC-15 (procedure §4.1): record a part installed during the work.
    The part reference and quantity are structured fields; stock levels
    are not touched here (receiving stock is `receive_part_order()`).
    """
    _require_workable_order(work_order)
    if part is None:
        raise ValidationError(_("Select the installed part (SVC-15)."))
    if quantity < 1:
        raise ValidationError(
            _("Part quantity must be at least 1 (SVC-15).")
        )
    used = UsedPart.objects.create(
        work_order=work_order, part=part, quantity=quantity
    )
    _log_system(
        work_order.case,
        user,
        "SVC-15: part '%s' x%s recorded as installed on work order #%s."
        % (part, quantity, work_order.pk),
    )
    return used


def send_completion_notice(
    case,
    user,
    *,
    works_performed: str,
    final_cost,
    handover_mode: str,
    scheduled_for,
    channel: str,
    cost_note: str = "",
) -> CompletionNotice:
    """
    SVC-17 (procedure §4.3 "Notifying the client"): notify the client
    that the work is finished. The structured fields (works performed,
    final cost, pickup/delivery + time, channel) are stored on a
    ``CompletionNotice`` and the formatted message is composed from them
    in the client language (NTF-03).

    Channel EMAIL sends immediately (``EmailLog`` row); channel SMS has
    no gateway - the drafted text is stored for staff to send from a
    phone and the case log records that it awaits manual sending.

    The "final cost" is checked against what the work order actually
    records (SVC-14's ``work_cost_so_far``). Departing from it is allowed
    - a discount, a goodwill write-off, a figure agreed on the phone are
    all real - but the reason is written down, because the alternative is
    a number the client was told that no record in the system supports.
    """
    from django.conf import settings
    from django.utils import translation

    from apps.notifications.services import (
        client_email_for,
        client_phone_for,
        send_client_email,
    )

    if not (works_performed or "").strip():
        raise ValidationError(_("Describe the works performed (SVC-17)."))
    if final_cost is None or final_cost <= 0:
        raise ValidationError(
            _("The final cost must be a positive amount in EUR (SVC-17).")
        )
    # §4.3 follows the control: "the work is finished" can only be said
    # once it has been done and checked. The notice went out at "New
    # report", and with a pickup on 01.01.2020 or on a Sunday (QA SV-M4).
    from apps.cases.services import has_reached_status
    from apps.scheduling.workinghours import (
        as_local,
        is_working_time,
        summary as working_hours_summary,
    )

    if not has_reached_status(case, "quality_check"):
        raise ValidationError(
            _(
                "The client is told the work is finished once it has passed "
                "the control - case #%(id)s is at '%(status)s' (SVC-17)."
            ) % {"id": case.pk, "status": case.status.name}
        )
    if scheduled_for is not None:
        if scheduled_for <= timezone.now():
            raise ValidationError(
                _("The pickup/delivery time has to be in the future (SVC-17).")
            )
        if not is_working_time(scheduled_for):
            raise ValidationError(
                _(
                    "%(when)s is outside the site's working hours "
                    "(SVC-17). %(hours)s"
                ) % {
                    "when": as_local(scheduled_for).strftime("%d.%m.%Y %H:%M"),
                    "hours": working_hours_summary(),
                }
            )

    # SVC-17 vs SVC-14. Unlike the ``quality_check`` guard, a cost that
    # cannot be totalled does NOT refuse here: that guard protects an
    # internal transition staff can retry after pricing a part, while
    # this one would leave the client uninformed about work that is
    # finished - a worse failure than an unverified figure. The note
    # becomes mandatory instead, so the gap is on the record either way.
    recorded_cost = None
    cost_problem = None
    try:
        recorded_cost = work_cost_so_far(case)
    except CostUnknown as exc:
        cost_problem = "; ".join(exc.messages)
    if not (cost_note or "").strip():
        if cost_problem is not None:
            raise ValidationError(
                _(
                    "The work on case #%(id)s cannot be totalled, so the "
                    "final cost cannot be checked - state why %(amount)s € "
                    "is the right figure. (%(problem)s)"
                ) % {
                    "id": case.pk,
                    "amount": final_cost,
                    "problem": cost_problem,
                }
            )
        if Decimal(final_cost) != recorded_cost:
            raise ValidationError(
                _(
                    "The work order for case #%(id)s records %(recorded)s €, "
                    "but the client would be told %(told)s € - correct the "
                    "figure or state why it differs (SVC-17)."
                ) % {
                    "id": case.pk,
                    "recorded": recorded_cost,
                    "told": final_cost,
                }
            )
    if handover_mode not in CompletionNotice.HandoverMode.values:
        raise ValidationError(
            _("Choose pickup or delivery for the asset (SVC-17).")
        )
    if scheduled_for is None:
        raise ValidationError(
            _("Set the pickup/delivery date and time (SVC-17).")
        )
    if channel not in CompletionNotice.Channel.values:
        raise ValidationError(
            _("Choose the notification channel: e-mail or SMS (SVC-17).")
        )
    if channel == CompletionNotice.Channel.EMAIL and not client_email_for(case):
        raise ValidationError(
            _(
                "Case #%(id)s has no client e-mail on file; add one or "
                "use the SMS channel (SVC-17)."
            ) % {"id": case.pk}
        )
    phone = client_phone_for(case)
    if channel == CompletionNotice.Channel.SMS and not phone:
        raise ValidationError(
            _(
                "Case #%(id)s has no client phone number on file; add one "
                "or use the e-mail channel (SVC-17)."
            ) % {"id": case.pk}
        )

    # Composed in the client language, never the staff UI language (NTF-03).
    with translation.override(settings.LANGUAGE_CODE):
        when = timezone.localtime(scheduled_for).strftime("%d.%m.%Y %H:%M")
        if handover_mode == CompletionNotice.HandoverMode.PICKUP:
            slot_line = _("Your asset is ready for pickup on: %(when)s.") % {
                "when": when
            }
        else:
            slot_line = _("Your asset will be delivered to you on: %(when)s.") % {
                "when": when
            }
        subject = _("Your asset is ready - work completed")
        body = "\n".join(
            [
                _("Hello,"),
                "",
                _("The work on your asset has been completed."),
                "",
                _("Works performed:"),
                works_performed.strip(),
                "",
                _("Final cost: %(amount)s €.") % {"amount": final_cost},
                slot_line,
                "",
                _("Thank you."),
            ]
        )

    notice = CompletionNotice.objects.create(
        case=case,
        works_performed=works_performed.strip(),
        final_cost=final_cost,
        handover_mode=handover_mode,
        scheduled_for=scheduled_for,
        channel=channel,
        cost_note=(cost_note or "").strip(),
        message=body,
        created_by=user,
    )
    if notice.cost_note:
        _log_system(
            case,
            user,
            "SVC-17: completion notice #%s states %s € against %s recorded "
            "on the work order. Reason: %s"
            % (
                notice.pk,
                final_cost,
                recorded_cost if recorded_cost is not None else "an untotalled work order",
                notice.cost_note,
            ),
        )
    if channel == CompletionNotice.Channel.EMAIL:
        if send_client_email(case, subject, body):
            notice.sent_at = timezone.now()
            notice.save(update_fields=["sent_at"])
        _log_system(
            case,
            user,
            "SVC-17: completion notice #%s composed and e-mailed (%s €, %s)."
            % (notice.pk, final_cost, handover_mode),
        )
    else:
        _log_system(
            case,
            user,
            "SVC-17: completion notice #%s drafted as SMS for %s - "
            "no SMS gateway, staff sends it manually (%s €, %s)."
            % (notice.pk, phone, final_cost, handover_mode),
        )
    return notice


@serialised_per_row
def record_test_run(
    work_order: WorkOrder,
    result: str,
    user,
    notes: str = "",
    *,
    kind: str = "",
    fault_cleared=None,
) -> TestRun:
    """
    SVC-16 (procedure §4.2 "Kontrola"): record a quality-control run. A
    FAIL sends the case back to rework (``quality_check -> in_progress``);
    a PASS leaves the case where it is so staff can move it to
    ``ready_for_handover`` (that transition's guard re-checks the latest
    run).

    The procedure names two controls - a field test in operating conditions, or an
    appropriate test run is performed - and two findings: that the fault
    is cleared, and that the asset is safe and functional. ``kind``
    records which control happened; ``fault_cleared`` is the first
    finding and ``result`` the second, because one does not follow from
    the other: an engine can run perfectly and still not be the reason
    the client came in.

    SVC-03: an asset worked on where it lies was never brought in, so a
    field test is not the control - only a test run is available there.
    """
    if kind not in TestRun.Kind.values:
        raise ValidationError(
            _(
                "Record which control was performed: the asset was "
                "checked in operating conditions, or a test run was carried "
                "out (SVC-16)."
            )
        )
    if kind == TestRun.Kind.FIELD_TEST and work_order.case.works_on_site:
        raise ValidationError(
            _(
                "Case #%(id)s is worked where the asset lies, so it was "
                "never lifted out and cannot be field-tested - record the "
                "test run that was performed instead (SVC-16)."
            ) % {"id": work_order.case_id}
        )
    if result == TestRun.Result.PASSED and fault_cleared is not True:
        raise ValidationError(
            _(
                "A quality control may only pass once the reported fault is "
                "confirmed cleared - if it is not, record the run as failed "
                "(SVC-16)."
            )
        )
    # §4.2: the control checks work that has been done. A PASS recorded
    # at "New report", before the asset had even arrived, later
    # satisfied the handover guard (QA SV-H2). The handover guard also
    # counts only runs made since the case last entered the control.
    if work_order.case.status.code not in ("in_progress", "quality_check"):
        raise ValidationError(
            _(
                "A test run is recorded on work that is under way or being "
                "checked - case #%(id)s is at '%(status)s' (SVC-16)."
            ) % {"id": work_order.case_id, "status": work_order.case.status.name}
        )
    test_run = TestRun.objects.create(
        work_order=work_order,
        result=result,
        kind=kind,
        fault_cleared=fault_cleared,
        notes=notes,
    )
    _log_system(
        work_order.case,
        user,
        "SVC-16: test run #%s recorded (%s, %s, fault cleared: %s)."
        % (
            test_run.pk,
            test_run.get_result_display(),
            test_run.get_kind_display(),
            "yes" if fault_cleared else "no",
        ),
    )
    if result == TestRun.Result.FAIL:
        case = work_order.case
        if case.status.code == "quality_check":
            # Imported lazily to keep the cross-app dependency one-way at
            # module level (cases resolves this app via apps.get_model).
            from apps.cases.services import transition_case

            transition_case(
                case,
                "in_progress",
                user,
                note="Quality-control test run #%s failed (SVC-16)." % test_run.pk,
            )
    return test_run


# ---------------------------------------------------------------------------
# SVC-14 (procedure §3.5): what the work has cost so far, against what the
# client approved. Both halves are read from data the business already
# maintains - the price list and the parts catalogue - so nothing here
# invents a rate.
# ---------------------------------------------------------------------------

class CostUnknown(ValidationError):
    """The work cannot be totalled because a price is missing.

    Deliberately not a silent zero: a part counted as free, or an hour
    counted at no rate, would defeat the comparison this exists for.
    """


def labour_rate():
    """The hourly rate, read from the LABOR row of the service price list.

    The price list is the business's own document (OPEN-05), so the rate
    is not configuration duplicated here. Exactly one active LABOR item
    must exist: none means the rate was never published, several mean the
    system cannot know which one applies, and both are for a human to fix
    rather than for this function to guess.
    """
    items = list(
        ServicePriceItem.objects.filter(
            category=ServicePriceItem.Category.LABOR,
            unit=ServicePriceItem.Unit.PER_HOUR,
            is_active=True,
        )
    )
    if len(items) != 1:
        raise CostUnknown(
            _(
                "The hourly labour rate cannot be read from the price list: "
                "%(count)s active items in the 'Labor rate' category, "
                "expected exactly one."
            ) % {"count": len(items)}
        )
    return items[0].price


def work_cost_so_far(case) -> Decimal:
    """Total the work recorded on ``case``'s work order (SVC-15).

    Hours from the work log at the price list's labour rate, plus each
    installed part at its catalogue price. Raises ``CostUnknown`` naming
    the parts that have no price - the caller turns that into a refusal,
    because an unpriced part makes the total meaningless rather than
    smaller.
    """
    work_order = getattr(case, "work_order", None)
    if work_order is None:
        return Decimal("0.00")

    total = Decimal("0.00")
    hours = sum(
        (entry.hours for entry in work_order.work_log.all()), Decimal("0.00")
    )
    if hours:
        total += hours * labour_rate()

    unpriced = []
    for used in work_order.used_parts.select_related("part"):
        if used.part.unit_price is None:
            unpriced.append(used.part.code)
            continue
        total += used.part.unit_price * used.quantity
    if unpriced:
        raise CostUnknown(
            _(
                "These installed parts have no price in the catalogue, so "
                "the work cannot be totalled: %(parts)s."
            ) % {"parts": ", ".join(sorted(unpriced))}
        )
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def approved_amount(case):
    """What the client (or the approval ladder) has authorised so far.

    Two ways into ``in_progress``, two authorities. Through the quote path
    it is the amount of the latest ACCEPTED quote that carries every
    approval its size requires. Through the SVC-10 shortcut there is no
    quote at all - the authority is the service's own predefined limit,
    which is exactly the TECHNICIAN row of the approval ladder.

    Returns ``None`` when neither exists, which the caller reports rather
    than treating as zero.
    """
    from apps.accounts.models import ApprovalLimit, Role
    from apps.quotes.models import Quote
    from apps.quotes.services import missing_approval_levels

    for quote in Quote.objects.filter(
        case=case, status=Quote.Status.ACCEPTED
    ).order_by("-created_at", "-pk"):
        if not missing_approval_levels(quote):
            return quote.amount

    limit = ApprovalLimit.objects.filter(role=Role.TECHNICIAN).first()
    return limit.max_amount if limit is not None else None
