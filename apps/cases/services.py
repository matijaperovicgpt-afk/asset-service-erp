"""
Case workflow state machine.

The workflow itself is data (``Status`` / ``StatusTransition``); this
module enforces it and runs guard checks (business preconditions) before
a case may enter certain statuses.

Guards are registered in ``GUARDS``: a dict mapping a target status code
to a list of guard callables ``(case, from_status) -> None`` that raise
``GuardCheckFailed`` when a precondition is not met. ``from_status`` lets
a guard apply only on specific edges (e.g. quote approval is required on
``awaiting_approval -> in_progress`` but not on the direct
``diagnostics -> in_progress`` shortcut, SVC-10/SVC-13). Add new guards
by appending callables to the registry.
"""
from datetime import timedelta

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .exceptions import (
    CaseWorkflowError,
    GuardCheckFailed,
    InvalidTransition,
    RoleNotAllowed,
)
from .models import (
    CONTACT_ROLES_WITH_OWN_DETAILS,
    ON_SITE_WORK_LOCATIONS,
    ActivityLog,
    Case,
    CaseType,
    ContactRole,
    Status,
    StatusHistory,
    StatusTransition,
)


# ---------------------------------------------------------------------------
# Guard checks
# ---------------------------------------------------------------------------

def _guard_intake_scheduled(case: Case, from_status: Status) -> None:
    """
    SVC-04: an intake appointment - the date AND the way the asset gets
    here - must be agreed with the client and recorded before the case is
    ``intake_scheduled``.

    The delivery method half is the newer one. This docstring claimed it
    for a long time while the code below checked only that an appointment
    existed; the procedure names it twice (§7's "agreed appointment and
    delivery method", and §8's "Intake scheduled - date and arrival
    method agreed"), so both halves are now asked for.

    §7.3 adds a third thing to the same conversation: where the asset
    actually is. It is asked before the delivery method because the
    delivery method is a function of it - you cannot agree who collects
    an asset from where until somebody says where.

    §7.1 adds the fourth, and it comes before both of them: *who* that
    conversation is with. §8 says as much about the status this guard
    protects - "New report: contact received, appointment not
    yet confirmed" - so the contact is what the case is meant to have
    settled by the time a date is. Both the location and the delivery
    method are things you learn from that person, which is why the check
    sits ahead of them.

    SVC-03: when the consult decided the job is done on site or on
    the drive leg, nothing is delivered anywhere - the technician goes
    out instead, and an ``ON_SITE_VISIT`` appointment is what gets
    agreed. Either kind satisfies the guard on such a case: what this
    gate is really asking is that a time was agreed with the client, and
    a visit booked under the older label is still an agreed time. The
    guide steers towards the right one; the guard does not block a case
    over the label.
    """
    Appointment = django_apps.get_model("scheduling", "Appointment")
    accepted = (
        [Appointment.Kind.ON_SITE_VISIT, Appointment.Kind.INTAKE]
        if case.works_on_site
        else [Appointment.Kind.INTAKE]
    )
    if not Appointment.objects.filter(case=case, kind__in=accepted).exists():
        if case.works_on_site:
            raise GuardCheckFailed(
                _(
                    "Cannot move case #%(id)s to 'intake_scheduled': the work "
                    "is done where the asset lies, so it needs an agreed "
                    "on-site visit (Appointment with kind=ON_SITE_VISIT)."
                ) % {"id": case.pk}
            )
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'intake_scheduled': missing an "
                "intake appointment (Appointment with kind=INTAKE)."
            ) % {"id": case.pk}
        )

    # §7.1: who we are agreeing all this with. The reporter is usually
    # the answer and one click says so; the point of asking is the case
    # where they are not.
    if not case.contact_person_recorded:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'intake_scheduled': nobody has "
                "said who we contact about it - the reporter, the owner, or "
                "somebody else (§7.1)."
            ) % {"id": case.pk}
        )

    # §7.3: and on an on-site case this matters more, not less - it is
    # the address the technician drives to.
    if not case.location.strip():
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'intake_scheduled': nobody has "
                "recorded where the asset is (§7.3)."
            ) % {"id": case.pk}
        )

    # SVC-04: no method to agree when nothing is delivered anywhere - the
    # technician goes out instead. ``delivery_method_recorded`` reads that
    # from SVC-03, so the two answers cannot contradict each other.
    if not case.delivery_method_recorded:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'intake_scheduled': the way "
                "the asset gets here has not been agreed - the date and "
                "the delivery method are settled in the same conversation "
                "(SVC-04)."
            ) % {"id": case.pk}
        )


def _guard_asset_received(case: Case, from_status: Status) -> None:
    """
    SVC-05: a SERVICE case may only enter ``asset_received`` once the
    work order carries both legal records - the scanned
    client-approval document AND the responsible technician's signature -
    and the intake record is signed.

    WNT-05: a STORAGE case requires a signed intake record (with
    item inventory and photos) but no work order.

    SVC-03: a SERVICE case whose work happens on site or on the
    drive leg never receives the asset at all, so no intake record can
    honestly be written for it - there is no mount status that is true
    and no receipt anyone could sign. Such a case still needs both work
    order records (the client approves the work wherever it is done) and
    still needs the asset photographed before anyone touches it
    (SVC-07), which is the part of the intake documentation that does
    survive. The status keeps its seeded name ("Asset received"), an
    internal label only - the client-visible one is "In service" either
    way.
    """
    missing = []

    if case.case_type == CaseType.SERVICE:
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        work_order = WorkOrder.objects.filter(
            case=case, is_preliminary=False, signed_at__isnull=False
        ).first()
        if work_order is None:
            missing.append(
                _(
                    "a final work order signed by the responsible technician "
                    "(is_preliminary=False, signed_at set)"
                )
            )
        else:
            from apps.service.services import has_client_approval

            if not has_client_approval(work_order):
                missing.append(
                    _(
                        "the client-approval scan (a DOCUMENT attachment on "
                        "the work order)"
                    )
                )

    if case.case_type == CaseType.SERVICE and not case.requires_asset_intake:
        Attachment = django_apps.get_model("attachments", "Attachment")
        from apps.attachments.services import attachments_for

        has_photo = attachments_for(case).filter(
            stage=Attachment.Stage.INTAKE, kind=Attachment.Kind.PHOTO
        ).exists()
        if not has_photo:
            missing.append(
                _(
                    "at least one photo of the asset taken at the visit "
                    "(SVC-07 still applies where the asset lies)"
                )
            )
    elif case.case_type in (CaseType.SERVICE, CaseType.STORAGE):
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        has_signed_intake = IntakeRecord.objects.filter(
            case=case, signed_at__isnull=False
        ).exists()
        if not has_signed_intake:
            missing.append(_("a signed intake record (signed_at set)"))

    # §7.2: "asset data" are obligatory data of the TICKET, so this
    # sits OUTSIDE the branches above - it is a property of the status,
    # not of the route taken to it.
    #
    # It was first written inside the on-site branch, on the reasoning
    # that an in-yard case is covered by ``sign_intake_record``. It is -
    # at SIGNING time. ``link_case`` can move a case to another client
    # afterwards, which drops an asset belonging to the previous one, and
    # the in-yard branch asks only whether a signed record exists: the
    # case then walked on holding a signed receipt for an asset it could no
    # longer name. Scoping a rule to the branch where its symptom was
    # found is how that happened.
    #
    # §7.6 is deliberately NOT mirrored here: the fuel gauge is part of
    # the intake record's list, and a case that writes no intake record has no
    # such list to leave blank.
    if case.case_type in (CaseType.SERVICE, CaseType.STORAGE):
        if case.asset is None:
            missing.append(
                _("a linked asset - triage has to say which asset this is (§7.2)")
            )
        elif not case.asset.is_fully_identified:
            missing.append(
                _(
                    "the asset's identification: %(fields)s (§7.2)"
                ) % {
                    "fields": ", ".join(
                        str(f) for f in case.asset.missing_identification()
                    )
                }
            )

    if missing:
        raise GuardCheckFailed(
            _("Cannot move case #%(id)s to 'asset_received': missing %(missing)s.")
            % {"id": case.pk, "missing": _(" and ").join(missing)}
        )


def _guard_diagnostics_recorded(case: Case, from_status: Status) -> None:
    """
    SVC-09: a diagnostic with all four mandatory pieces (the issue,
    servicing work, parts needed and estimated completion date) must be
    recorded on the work order before the case leaves ``diagnostics``.
    Applies only on edges out of ``diagnostics``.
    """
    if from_status.code != "diagnostics":
        return

    Diagnostic = django_apps.get_model("service", "Diagnostic")
    if not Diagnostic.complete_for_case(case).exists():
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s out of 'diagnostics': the work "
                "order needs a diagnostic with the issue, servicing work, "
                "parts needed and estimated completion date recorded."
            ) % {"id": case.pk}
        )

def _guard_new_client_inspection(case: Case, from_status: Status) -> None:
    """
    SVC-08: for a first-time client, the detailed full inspection of the
    whole asset and all relevant service points must be performed and
    signed before a final quote is issued - i.e. before the case leaves
    ``diagnostics`` for the approval/quote phase. Returning clients and
    non-SERVICE cases are unaffected.

    Applies only on ``diagnostics -> awaiting_approval`` (registered under
    that target). The direct ``diagnostics -> in_progress`` shortcut
    (SVC-10, a stock consumable within the approval limit) issues no quote
    and is intentionally not guarded here.
    """
    if from_status.code != "diagnostics":
        return
    if case.case_type != CaseType.SERVICE:
        return

    from apps.service.services import inspection_complete_for_case, is_new_client

    if not is_new_client(case):
        return
    if not inspection_complete_for_case(case):
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s out of 'diagnostics': a first-time "
                "client requires a signed full inspection of the asset "
                "before a final quote is issued (SVC-08)."
            ) % {"id": case.pk}
        )


def _guard_quote_approved(case: Case, from_status: Status) -> None:
    """
    SVC-13 / INV-04: work waiting on client consent may only resume
    (``awaiting_approval -> in_progress``) once the latest quote is
    accepted and carries every approval its amount requires per
    ``ApprovalLimit``. The direct ``diagnostics -> in_progress`` shortcut
    (SVC-10) is intentionally not guarded here.

    ``awaiting_part -> in_progress`` is guarded too. The only way into
    ``awaiting_part`` is from ``awaiting_approval``, so a case waiting on
    a part is still a case waiting on consent. Checking only the direct
    edge let a stocked "order" carry a case with a DRAFT quote round
    the approval and straight into work (QA SV-C1).
    """
    if from_status.code not in ("awaiting_approval", "awaiting_part"):
        return

    from apps.quotes.services import latest_quote, missing_approval_levels

    Quote = django_apps.get_model("quotes", "Quote")
    quote = latest_quote(case)
    if quote is None:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_progress': no quote exists "
                "for the client to approve."
            ) % {"id": case.pk}
        )
    if quote.status != Quote.Status.ACCEPTED:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_progress': the latest quote "
                "(#%(quote)s) is not accepted (status is '%(status)s')."
            ) % {"id": case.pk, "quote": quote.pk, "status": quote.status}
        )
    missing = missing_approval_levels(quote)
    if missing:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_progress': quote #%(quote)s "
                "of %(amount)s %(currency)s is missing approval(s): %(levels)s."
            ) % {
                "id": case.pk,
                "quote": quote.pk,
                "amount": quote.amount,
                "currency": quote.currency,
                "levels": ", ".join(missing),
            }
        )


def _guard_needed_parts_are_stock(case: Case, diagnostic) -> None:
    """SVC-10, the half that was free text until now: every part the
    diagnostic names is a consumable, held in the quantity required."""
    not_consumable = []
    short = []
    for needed in diagnostic.needed_parts.select_related("part"):
        part = needed.part
        if not part.is_consumable:
            not_consumable.append(part.code)
        elif part.stock_qty < needed.quantity:
            short.append(
                "%s (%s needed, %s in stock)"
                % (part.code, needed.quantity, part.stock_qty)
            )
    if not_consumable:
        raise GuardCheckFailed(
            _(
                "Cannot take the direct route to 'in_progress' on case "
                "#%(id)s: %(parts)s is not a regular consumable, so the case "
                "belongs on the quote path (SVC-10)."
            ) % {"id": case.pk, "parts": ", ".join(sorted(not_consumable))}
        )
    if short:
        raise GuardCheckFailed(
            _(
                "Cannot take the direct route to 'in_progress' on case "
                "#%(id)s: the store does not hold enough of %(parts)s, so it "
                "has to be ordered (SVC-10)."
            ) % {"id": case.pk, "parts": ", ".join(sorted(short))}
        )
    if diagnostic.parts_needed.strip():
        raise GuardCheckFailed(
            _(
                "Cannot take the direct route to 'in_progress' on case "
                "#%(id)s: diagnostic #%(diag)s names parts the catalogue does "
                "not know, and an unidentified part cannot be shown to be a "
                "consumable in stock. Add them to the catalogue, or take the "
                "quote path (SVC-10)."
            ) % {"id": case.pk, "diag": diagnostic.pk}
        )


def _guard_stock_shortcut(case: Case, from_status: Status) -> None:
    """
    SVC-10 (procedure §3.2): the service may start work without a quote
    only when the fault is known, the part is a regular consumable in
    stock, and the cost is inside the predefined approval limit. This
    guards the ``diagnostics -> in_progress`` shortcut, which used to be
    labelled with that condition in the wizard while checking none of it.

    All three are checkable now. The **cost** is the diagnostic's own
    estimate for the parts, measured against the ladder in
    ``ApprovalLimit``: if the amount is large enough to need a MANAGER or
    DIRECTOR signature, then by definition it is not "within the
    predefined limit" and the case belongs on the quote path.

    The **parts** are read from the diagnostic's own ``DiagnosticPart``
    rows: each has to be a consumable the store holds in the quantity the
    job needs. This was unverifiable while the parts lived in free text
    with no reference to look up, so the guard could only check stock
    *negatively* - through the part orders somebody had already raised.
    That negative check is kept: it still catches a case where the order
    exists but the diagnostic never named the part.

    Free text left over means the catalogue has no row for something, and
    an unidentified part cannot be shown to be a consumable in stock. It
    is not a refusal to *record* the diagnosis - it costs the case the
    shortcut, not the diagnosis.
    """
    if from_status.code != "diagnostics" or case.case_type != CaseType.SERVICE:
        return

    from apps.quotes.models import Approval
    from apps.quotes.services import required_approval_levels

    Diagnostic = django_apps.get_model("service", "Diagnostic")
    diagnostic = (
        Diagnostic.complete_for_case(case).order_by("-created_at", "-pk").first()
    )
    if diagnostic is None:
        return  # _guard_diagnostics_recorded already refuses this

    internal = [
        level
        for level in required_approval_levels(diagnostic.estimated_parts_price)
        if level != Approval.Level.CLIENT
    ]
    if internal:
        raise GuardCheckFailed(
            _(
                "Cannot take the direct route to 'in_progress' on case "
                "#%(id)s: the estimated parts cost of %(amount)s EUR is above "
                "the limit the service may approve on its own and needs "
                "%(levels)s approval - quote it to the client instead "
                "(SVC-10)."
            ) % {
                "id": case.pk,
                "amount": diagnostic.estimated_parts_price,
                "levels": ", ".join(internal),
            }
        )

    # Cost first, then the parts: the limit is the coarser gate, and a case
    # over it belongs on the quote path whatever it happens to need.
    _guard_needed_parts_are_stock(case, diagnostic)

    PartOrder = django_apps.get_model("parts", "PartOrder")
    from apps.parts.services import is_stock_part

    bought = [
        order
        for order in PartOrder.objects.filter(
            case=case, received_at__isnull=True
        ).select_related("part")
        if not is_stock_part(order.part)
    ]
    if bought:
        raise GuardCheckFailed(
            _(
                "Cannot take the direct route to 'in_progress' on case "
                "#%(id)s: %(parts)s had to be ordered rather than taken from "
                "stock, so the work needs a quote and the client's consent "
                "(SVC-10/SVC-11)."
            ) % {
                "id": case.pk,
                "parts": ", ".join(sorted(o.part.code for o in bought)),
            }
        )


def _guard_extra_work_approved(case: Case, from_status: Status) -> None:
    """
    SVC-14 (procedure §3.5): "Any additional work or cost increase not
    covered by the original approval must be confirmed with the client
    beforehand." Work may only be declared finished while what it cost
    is still inside what was approved.

    The point of putting this on ``in_progress -> quality_check`` is that
    it is the last moment the word *prethodno* still means anything: past
    it the asset is tested, the client is notified and the final cost is
    typed by hand. The remedy is the edge that already exists in the
    other direction - back to ``awaiting_approval`` with a new quote.

    A price that cannot be read is a refusal, not a zero: see
    ``service.services.work_cost_so_far``.
    """
    if from_status.code != "in_progress" or case.case_type != CaseType.SERVICE:
        return

    from apps.service.services import (
        CostUnknown,
        approved_amount,
        work_cost_so_far,
    )

    try:
        spent = work_cost_so_far(case)
    except CostUnknown as exc:
        raise GuardCheckFailed(
            _("Cannot finish work on case #%(id)s: %(why)s")
            % {"id": case.pk, "why": " ".join(exc.messages)}
        ) from exc

    approved = approved_amount(case)
    if approved is None:
        raise GuardCheckFailed(
            _(
                "Cannot finish work on case #%(id)s: nothing authorises the "
                "work - there is no fully approved quote and no technician "
                "approval limit to fall back on (SVC-14)."
            ) % {"id": case.pk}
        )
    if spent > approved:
        raise GuardCheckFailed(
            _(
                "Cannot finish work on case #%(id)s: the work recorded so far "
                "comes to %(spent)s EUR against %(approved)s EUR approved. "
                "Additional work must be agreed with the client first - send "
                "a new quote (SVC-14)."
            ) % {"id": case.pk, "spent": spent, "approved": approved}
        )


def _guard_awaiting_part(case: Case, from_status: Status) -> None:
    """
    SVC-14: ``awaiting_part`` requires an open part order with an
    expected delivery date. SVC-11 / INV-05: any open order for a
    non-stock part must be backed by a linked 50% advance payment.
    """
    from apps.parts.services import is_stock_part

    PartOrder = django_apps.get_model("parts", "PartOrder")
    open_orders = list(
        PartOrder.objects.filter(case=case, received_at__isnull=True).select_related("part")
    )
    if not any(order.expected_delivery for order in open_orders):
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'awaiting_part': no open part "
                "order with an expected delivery date."
            ) % {"id": case.pk}
        )
    unbacked = [
        order
        for order in open_orders
        if not is_stock_part(order.part) and order.advance_payment_id is None
    ]
    if unbacked:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'awaiting_part': part order(s) "
                "%(orders)s are for non-stock parts without a linked 50%% "
                "advance payment."
            ) % {
                "id": case.pk,
                "orders": ", ".join("#%s" % order.pk for order in unbacked),
            }
        )


def _guard_ready_for_handover(case: Case, from_status: Status) -> None:
    """
    SVC-16: quality control must pass before the asset is offered back
    to the client - the latest test run on the work order must be PASS.
    """
    TestRun = django_apps.get_model("service", "TestRun")
    latest_run = latest_quality_control_run(case)
    if latest_run is None:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'ready_for_handover': no "
                "quality-control test run recorded."
            ) % {"id": case.pk}
        )
    if latest_run.result != TestRun.Result.PASSED:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'ready_for_handover': the latest "
                "test run (#%(run)s) did not pass."
            ) % {"id": case.pk, "run": latest_run.pk}
        )


def status_reached_at(case: Case, code: str):
    """When ``case`` last entered status ``code`` (``None`` if never)."""
    return (
        StatusHistory.objects.filter(case=case, to_status__code=code)
        .order_by("-changed_at")
        .values_list("changed_at", flat=True)
        .first()
    )


def latest_quality_control_run(case: Case):
    """
    The newest test run of the CURRENT quality control: one recorded
    before the work (or before a rework) says nothing about it (QA SV-H2).
    Shared by the ``ready_for_handover`` guard and the guide so the
    checklist cannot tick a run the transition will refuse.
    """
    TestRun = django_apps.get_model("service", "TestRun")
    runs = TestRun.objects.filter(work_order__case=case)
    entered_qc = status_reached_at(case, "quality_check")
    if entered_qc is not None:
        runs = runs.filter(performed_at__gte=entered_qc)
    return runs.order_by("-performed_at", "-pk").first()


def has_reached_status(case: Case, code: str) -> bool:
    """
    Has ``case`` reached the status ``code`` (or gone past it)?

    Compares ``Status.sort_order`` rather than a hardcoded list of codes,
    because the workflow is data: which statuses exist and in what order
    is a property of the seeded rows, not of this module. A code that
    does not exist for the case's type answers ``False`` - an unknown
    milestone has not been reached.
    """
    target = Status.objects.filter(case_type=case.case_type, code=code).first()
    if target is None:
        return False
    return case.status.sort_order >= target.sort_order


def _guard_quote_sent(case: Case, from_status: Status) -> None:
    """
    TRP-01: on a transport case the offer - scope of service and
    estimated cost - goes out BEFORE any transport is organized, and it
    is built on a documented inquiry.

    This is the rule the guide has always claimed to block on. It did
    not: a blocking StepDef only greys out the command-bar button, while
    the Transitions accordion on the same page renders a live one, and
    ``auto_advance_case`` walks the linear chain on guards alone. So a
    single offer POST could carry a case two statuses forward with the
    quote still DRAFT - marking an offer as sent that never was.
    """
    if case.case_type != CaseType.TRANSPORT:
        return

    from apps.transport.services import (
        inquiry_is_documented,
        missing_inquiry_data,
        payable_transport_quote,
    )

    order = getattr(case, "transport_order", None)
    if not inquiry_is_documented(order):
        missing = (
            missing_inquiry_data(order)
            if order is not None
            else [_("the transport inquiry")]
        )
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'quote_sent': the inquiry is "
                "incomplete - missing %(missing)s. The offer is built on "
                "it (TRP-01)."
            ) % {"id": case.pk, "missing": ", ".join(str(m) for m in missing)}
        )

    # "Payable" is the same notion TRP-04 derives the advance from: a
    # quote that has left DRAFT and was not rejected. Sharing it keeps
    # the guard and the advance from disagreeing about what "the offer"
    # is - which is exactly how a case used to reach ``advance_paid``
    # with nothing to compute an advance from.
    if payable_transport_quote(case) is None:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'quote_sent': no offer has "
                "been sent - a quote stating the scope of service and the "
                "estimated cost is issued before the transport is "
                "organized (TRP-01)."
            ) % {"id": case.pk}
        )


def _guard_advance_paid(case: Case, from_status: Status) -> None:
    """
    TRP-04: a transport case may not claim the advance stage before an
    advance exists to be paid.

    The advance is derived from the offer, never typed
    (``transport.services.ensure_transport_advance``), so in the normal
    flow sending the offer creates it and this guard is already
    satisfied. It exists for the case that skipped that: the guide's
    ``request_advance`` step has always said it blocks, and this is what
    makes that true.

    The status says the advance is *paid*, so an unpaid row is not
    enough: sending the offer used to create the advance and auto-advance
    the case to "Advance paid" in the same click (QA T-M1).
    """
    if case.case_type != CaseType.TRANSPORT:
        return
    if not case.advance_payments.exists():
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'advance_paid': no advance "
                "payment exists yet - it is derived from the offer when "
                "the offer is sent (TRP-04)."
            ) % {"id": case.pk}
        )
    if not case.advance_payments.filter(paid_at__isnull=False).exists():
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'advance_paid': the advance "
                "has not been marked paid yet (TRP-04)."
            ) % {"id": case.pk}
        )


def _guard_departure_scheduled(case: Case, from_status: Status) -> None:
    """
    TRP-04 / INV-06: a transport departure may only be scheduled once an
    advance payment is actually paid, and it has to be the advance the
    procedure asks for - 50% of the offer, not any paid row of any size.
    If it was paid later than 7 days before the planned departure this is
    logged as a warning on the case (the transition is not blocked).

    TRP-03: for an asset own transport cannot carry, the specialized
    carrier's own separate offer is obtained before the
    transport is organized - and scheduling the departure is exactly when
    it is being organized.
    """
    _guard_carrier_quote(case, from_status)
    AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
    advance = (
        AdvancePayment.objects.filter(case=case, paid_at__isnull=False)
        .order_by("-paid_at")
        .first()
    )
    if advance is None:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'departure_scheduled': no paid "
                "advance payment recorded (paid_at set)."
            ) % {"id": case.pk}
        )

    # TRP-04 "50% advance": the guard used to ask only whether SOME
    # advance was paid, so a 10% row marked paid reserved the truck, the
    # driver and the crane while the real half sat unpaid on the same
    # case. Compare, never correct - a paid advance is a recorded fact
    # and ``ensure_transport_advance`` deliberately never rewrites one.
    if case.case_type == CaseType.TRANSPORT:
        from apps.transport.services import (
            derived_advance_amount,
            payable_transport_quote,
        )

        quote = payable_transport_quote(case)
        if quote is None:
            # A DRAFT revision on top retires nothing until it is sent:
            # measure against the last offer that did go out. Skipping
            # the check here let any draft wave a 1% advance through
            # (QA T-H2).
            Quote = django_apps.get_model("quotes", "Quote")
            quote = (
                case.quotes.filter(
                    status__in=[Quote.Status.SENT, Quote.Status.ACCEPTED]
                )
                .order_by("-created_at", "-pk")
                .first()
            )
        if quote is not None:
            required = derived_advance_amount(quote)
            paid_total = sum(
                a.amount
                for a in AdvancePayment.objects.filter(
                    case=case, paid_at__isnull=False
                )
            )
            if paid_total < required:
                raise GuardCheckFailed(
                    _(
                        "Cannot move case #%(id)s to 'departure_scheduled': "
                        "the advance paid is %(paid)s of the %(required)s "
                        "that 50%% of the offer comes to - the vehicle, "
                        "driver and crane are reserved against the full "
                        "advance (TRP-04)."
                    ) % {
                        "id": case.pk,
                        "paid": paid_total,
                        "required": required,
                    }
                )

    departure_date = None
    Appointment = django_apps.get_model("scheduling", "Appointment")
    departure_appointment = (
        Appointment.objects.filter(
            case=case, kind=Appointment.Kind.TRANSPORT_DEPARTURE
        )
        .order_by("scheduled_for")
        .first()
    )
    # "Departure scheduled" names a departure date - a booked appointment, or
    # at least the date agreed on the inquiry. The status was reached with
    # neither (QA T-M2).
    if (
        case.case_type == CaseType.TRANSPORT
        and departure_appointment is None
        and not getattr(getattr(case, "transport_order", None), "preferred_date", None)
    ):
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'departure_scheduled': no "
                "departure date has been agreed or booked yet (TRP-05)."
            ) % {"id": case.pk}
        )
    if departure_appointment is not None:
        departure_date = timezone.localdate(departure_appointment.scheduled_for)
    else:
        transport_order = getattr(case, "transport_order", None)
        if transport_order is not None and transport_order.preferred_date:
            departure_date = transport_order.preferred_date

    paid_on = timezone.localdate(advance.paid_at)
    if departure_date is not None and paid_on > departure_date - timedelta(days=7):
        ActivityLog.objects.create(
            case=case,
            kind=ActivityLog.Kind.SYSTEM,
            text=(
                "TRP-04 warning: advance payment #%s was paid on %s, later "
                "than 7 days before the planned departure on %s."
                % (advance.pk, paid_on, departure_date)
            ),
        )


def _guard_in_transit(case: Case, from_status: Status) -> None:
    """
    TRP-06: before a transport asset may leave (``in_transit``), the
    pre-transport intake record ("intake record pre transporta") must be
    signed. It documents the asset state, equipment, personal items,
    documents and visible damage, with arrival photos, and follows the
    asset to final delivery. Signing the record already requires an
    INTAKE photo (SVC-07), so a signed record implies the photos exist.
    """
    if case.case_type != CaseType.TRANSPORT:
        return
    IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
    has_signed_intake = IntakeRecord.objects.filter(
        case=case, signed_at__isnull=False
    ).exists()
    if not has_signed_intake:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_transit': missing a signed "
                "pre-transport intake record - it must be made "
                "before loading (TRP-06)."
            ) % {"id": case.pk}
        )

    # TRP-02: the loading/unloading plan is agreed before the asset
    # moves, not discovered on arrival. Without it a truck can be sent to
    # a site where no crane is available and nobody is expecting it -
    # which is the whole reason the procedure agrees these five things in
    # advance.
    from apps.transport.services import loading_plan_is_agreed, missing_plan_items

    order = getattr(case, "transport_order", None)
    if not loading_plan_is_agreed(order):
        missing = missing_plan_items(
            getattr(order, "loading_plan", None) if order else None
        )
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_transit': the loading and "
                "unloading plan is not agreed - missing %(missing)s "
                "(TRP-02)."
            ) % {"id": case.pk, "missing": ", ".join(str(m) for m in missing)}
        )

    # TRP-07: the asset leaves once it is loaded and checked for the
    # road, not once the paperwork for it exists. Without this a saved
    # plan moved a case "U transportu" weeks before departure (QA T-H1).
    confirmation = getattr(order, "loading_confirmation", None)
    LoadingConfirmation = django_apps.get_model("transport", "LoadingConfirmation")
    if (
        confirmation is None
        or confirmation.signed_at is None
        or confirmation.readiness != LoadingConfirmation.Readiness.READY
    ):
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_transit': the loading has "
                "not been confirmed ready for the road (TRP-07)."
            ) % {"id": case.pk}
        )

    Appointment = django_apps.get_model("scheduling", "Appointment")
    departure = (
        Appointment.objects.filter(
            case=case, kind=Appointment.Kind.TRANSPORT_DEPARTURE
        )
        .order_by("scheduled_for")
        .first()
    )
    if departure is not None and (
        timezone.localdate(departure.scheduled_for) > timezone.localdate()
    ):
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_transit': the departure is "
                "booked for %(date)s, which is still ahead."
            ) % {
                "id": case.pk,
                "date": timezone.localdate(departure.scheduled_for).strftime(
                    "%d.%m.%Y."
                ),
            }
        )


def _guard_carrier_quote(case: Case, from_status: Status) -> None:
    """
    TRP-03: "Larger assets are moved by specialized carriers, from whom
    a separate quote is obtained."

    The length rule itself stays advisory - "when dimensions and
    regulations allow" is a judgement, and ``validate_mode`` warns rather than
    refuses. What is not a judgement is that when an external carrier IS
    doing the job, their offer is obtained. The declare-price form has
    always been on the case page; nothing ever asked for it, so a 14 m
    asset could reach ``delivered`` with no carrier named and no carrier
    price - the one commercial fact this sub-step exists to capture.
    """
    if case.case_type != CaseType.TRANSPORT:
        return
    from apps.transport.services import carrier_quote_required

    order = getattr(case, "transport_order", None)
    if not carrier_quote_required(order):
        return
    if order.carrier_price_declared_at is None:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'departure_scheduled': this "
                "asset goes with a specialized carrier, whose own offer "
                "is obtained before the transport is organized - declare "
                "the carrier's price first (TRP-03)."
            ) % {"id": case.pk}
        )


def _guard_delivered(case: Case, from_status: Status) -> None:
    """
    TRP-08: a transport case only reaches ``delivered`` once the delivery
    protocol is recorded - unloading/launch organized, the owner or an
    authorized representative named as attendee and the staff sign-off
    stamped (``record_delivery_confirmation`` signs on submit, so an
    existing confirmation implies the sign-off).
    """
    if case.case_type != CaseType.TRANSPORT:
        return
    DeliveryConfirmation = django_apps.get_model(
        "transport", "DeliveryConfirmation"
    )
    has_confirmation = DeliveryConfirmation.objects.filter(
        transport_order__case=case
    ).exists()
    if not has_confirmation:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'delivered': missing the "
                "delivery confirmation - record the "
                "delivery protocol first (TRP-08)."
            ) % {"id": case.pk}
        )


def _guard_contracted(case: Case, from_status: Status) -> None:
    """
    WNT-01 / WNT-03 / INV-11: a storage case is contracted *before*
    the priced estimate goes out, so the inquiry must be fully documented
    and price-ready first. Required: a client contact (phone or e-mail),
    a linked asset (length is mandatory on the asset record and drives
    pricing), and - on the storage agreement - the permit number,
    the asset length, the accommodation and the storage term. With those
    four pricing inputs present the value estimate can be computed
    dynamically once the case is contracted.

    WNT-02 (procedure §5.2) names five things that go into the database:
    length, site location, ownership, contact and the selected
    services. Three of them - the site, the ownership and the services -
    used to be demanded only by the WNT-02 "database entry" page, and
    nothing routes a case through that page: the agreement card on the
    case detail supplies the permit, length, accommodation and term all
    by itself, so a case could be contracted with the other three blank.
    They are checked here now, where the status change is, rather than on
    one of the several forms that can write an agreement.
    """
    if case.case_type != CaseType.STORAGE:
        return

    missing = []
    has_contact = bool(case.raw_phone or case.raw_email)
    if not has_contact and case.client_id is not None:
        has_contact = bool(case.client.phone or case.client.email)
    if not has_contact:
        missing.append(_("a client contact (phone or e-mail)"))
    if case.asset_id is None:
        missing.append(
            _("an asset linked to the case (length drives storage pricing)")
        )

    agreement = getattr(case, "storage_agreement", None)
    if agreement is None or not agreement.permit_no:
        missing.append(_("the permit number on the storage agreement"))
    if agreement is None or agreement.asset_length_m is None:
        missing.append(_("the asset length on the storage agreement"))
    if agreement is None or not agreement.accommodation:
        missing.append(_("the accommodation (open yard or hall)"))
    if agreement is None or not agreement.term:
        missing.append(_("the storage term (daily, monthly or seasonal)"))
    if agreement is None or not agreement.origin_site:
        missing.append(_("the site the asset is coming from"))
    if agreement is None or not agreement.ownership:
        missing.append(_("the ownership of the asset"))
    if agreement is None or not agreement.selected_services.exists():
        missing.append(_("at least one selected service"))

    if missing:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'contracted': missing "
                "%(missing)s (WNT-01)."
            ) % {"id": case.pk, "missing": _(" and ").join(missing)}
        )


def _guard_estimate_sent(case: Case, from_status: Status) -> None:
    """
    WNT-03: 'Procjena poslata' (estimate sent) means the dynamically
    computed value estimate has actually gone to the client, so the case
    may only enter ``estimate_sent`` once a sent (non-draft) quote exists
    on it. Staff draft the estimate after contracting, adjust the amount,
    then send it. Scoped to STORAGE.
    """
    if case.case_type != CaseType.STORAGE:
        return
    Quote = django_apps.get_model("quotes", "Quote")
    sent = Quote.objects.filter(case=case).exclude(status="draft")
    # The estimate is the one drafted *after* contracting (WNT-03). An
    # offer sent while the case was still an inquiry used to satisfy this
    # and carry contracting straight past the estimate step (QA S-M1).
    contracted_at = status_reached_at(case, "contracted")
    if contracted_at is not None:
        sent = sent.filter(created_at__gte=contracted_at)
    has_sent = sent.exists()
    if not has_sent:
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'estimate_sent': no value "
                "estimate has been sent to the client yet (WNT-03)."
            ) % {"id": case.pk}
        )


def _guard_in_storage(case: Case, from_status: Status) -> None:
    """
    WNT-05 / WNT-06: "In storage" means the asset stands on its
    spot. The status had no guard of its own, so signing the intake record
    carried a case through it on the same click, with the asset never
    placed anywhere (QA S-H1).
    """
    if case.case_type != CaseType.STORAGE:
        return
    agreement = getattr(case, "storage_agreement", None)
    if agreement is None or not agreement.positions.exists():
        raise GuardCheckFailed(
            _(
                "Cannot move case #%(id)s to 'in_storage': the asset has "
                "not been placed on the yard or in the hall yet (WNT-05)."
            ) % {"id": case.pk}
        )


def _guard_closed(case: Case, from_status: Status) -> None:
    """
    SVC-19 / INV-07: any case may only be closed once the asset handover
    is signed and the case is financially settled.

    SVC-03: a case worked on site or in the field has no
    handover to sign - the handover record documents *returning* an
    asset, compared item by item against the intake record, and neither
    document exists when the asset never left its site. Financial
    settlement still gates the close, and so does the quality control
    upstream (SVC-16), which is what the procedure actually checks at
    the end of an intervention.
    """
    missing = []

    if case.requires_asset_intake:
        HandoverRecord = django_apps.get_model("intake", "HandoverRecord")
        has_signed_handover = HandoverRecord.objects.filter(
            case=case, signed_at__isnull=False
        ).exists()
        if not has_signed_handover:
            missing.append(_("a signed handover record (signed_at set)"))

    if not case.financially_settled:
        missing.append(_("financial settlement (financially_settled=True)"))

    if missing:
        raise GuardCheckFailed(
            _("Cannot move case #%(id)s to 'closed': missing %(missing)s.")
            % {"id": case.pk, "missing": _(" and ").join(missing)}
        )


def record_delivery_method(
    case: Case, delivery_method: str, user, *, note: str = ""
) -> Case:
    """
    SVC-04: record how the asset reaches the yard - the other half of
    what "Intake scheduled" means.

    Re-recording is allowed: staff correct a wrong answer, and a client
    who said they would trailer it themselves and then asks us to collect
    it is an ordinary Tuesday. The change is logged rather than blocked,
    because the log is what makes a late change visible to whoever
    arranged the crane.

    Refuses an on-site case: when the job is done where the asset lies
    nothing is delivered anywhere, and recording a delivery method there
    would contradict the SVC-03 answer rather than complete it.
    """
    from .models import DeliveryMethod

    if delivery_method not in DeliveryMethod.values:
        raise ValidationError(
            _("Choose how the asset reaches the yard (SVC-04).")
        )

    if case.works_on_site:
        raise ValidationError(
            _(
                "Case #%(id)s is worked where the asset lies, so nothing "
                "is delivered anywhere - change the work location first if "
                "the asset is coming in after all (SVC-03/SVC-04)."
            ) % {"id": case.pk}
        )

    previous = case.delivery_method
    if previous == delivery_method:
        return case

    case.delivery_method = delivery_method
    case.save(update_fields=["delivery_method"])

    label = case.get_delivery_method_display()
    if previous:
        text = (
            "SVC-04: delivery method changed from '%s' to '%s'"
            % (dict(DeliveryMethod.choices).get(previous, previous), label)
        )
    else:
        text = "SVC-04: delivery method recorded as '%s'" % label
    if note:
        text += " - %s" % note
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text + ".",
    )
    return case


def _clear_delivery_method_for_on_site(case: Case, user) -> None:
    """SVC-03 supersedes SVC-04: nothing is delivered to a yard the asset
    never enters.

    ``delivery_method_recorded`` reads ``works_on_site``, so the guard
    was already satisfied either way - but the column kept whatever was
    agreed before the consult, and a case could read "we collect it"
    while the consult said the technician drives out. Whoever booked the
    crane on the strength of the old answer needs to see it retired,
    which is why this logs rather than blanking quietly.
    """
    if not case.works_on_site or not case.delivery_method:
        return
    previous = case.get_delivery_method_display()
    case.delivery_method = ""
    case.save(update_fields=["delivery_method"])
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-04: delivery method '%s' cleared - the SVC-03 consult "
            "put the work where the asset lies, so there is nothing to "
            "deliver." % previous
        ),
    )


def record_contact_person(
    case: Case,
    user,
    *,
    role: str,
    name: str = "",
    phone: str = "",
    email: str = "",
    note: str = "",
) -> Case:
    """
    §7.1: designate the *contact person* - who we ring about this case.

    The procedure names three roles and the application had homes for
    two: the ``raw_*`` trio is the reporter, ``Case.client`` is the
    owner. Usually all three are one person, and REPORTER says so in one
    click. The reason to ask at all is the case where they are not, and
    the reason it is not cosmetic is that ``contact_details()`` - and
    therefore every client e-mail and every manual call - resolves
    through the designation.

    A contact nobody can reach is not a contact, so each role is checked
    against the data it points at: OWNER needs a linked client with a
    phone or an e-mail, REPORTER needs the raw trio to hold a name and
    one of the two, and the two roles that carry their own details need
    them supplied. Refusing here rather than discovering it at the first
    unsendable notification is the whole point of the check.

    Choosing a role that resolves to data on the case CLEARS the
    override fields: a number left behind by a superseded designation
    would sit in the database looking like current information.

    Re-designating is allowed and logged as a change - a skipper leaves,
    an owner comes back from abroad, and whoever is about to ring the
    old number needs to see that it happened.
    """
    role = (role or "").strip()
    if role not in ContactRole.values:
        raise ValidationError(
            _("Unknown contact role: %(role)s.") % {"role": role or "-"}
        )

    name = (name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip()

    if role in CONTACT_ROLES_WITH_OWN_DETAILS:
        if not name:
            raise ValidationError(_("Name the person we should contact (§7.1)."))
        if not (phone or email):
            raise ValidationError(
                _(
                    "Give the contact a phone number or an e-mail address - "
                    "a contact person nobody can reach is not one (§7.1)."
                )
            )
        if email:
            # The shape of the address is checked HERE and not only on
            # the form. A rule enforced by one form is enforced only for
            # people who open that form, and this one guards the single
            # value every client e-mail is addressed to - an unparseable
            # string here fails silently at send time, inside the
            # try/except that keeps a broken mail setup from breaking a
            # status change.
            try:
                validate_email(email)
            except ValidationError as exc:
                raise ValidationError(
                    _("'%(email)s' is not a valid e-mail address (§7.1).")
                    % {"email": email}
                ) from exc
    else:
        # The designation points at data the case already holds; anything
        # typed into the override fields would be ignored by the
        # resolver, so it is dropped rather than stored misleadingly.
        name = phone = email = ""

    if role == ContactRole.OWNER:
        client = case.client
        if client is None:
            raise ValidationError(
                _(
                    "This case has no client linked yet, so the owner cannot "
                    "be the contact - match the client at triage first (§7.1)."
                )
            )
        if not (client.phone or client.email):
            raise ValidationError(
                _(
                    "%(client)s has neither a phone number nor an e-mail "
                    "address on file, so nothing could be sent there (§7.1)."
                ) % {"client": client.name}
            )
    elif role == ContactRole.REPORTER:
        if not case.raw_contact_name.strip():
            raise ValidationError(
                _(
                    "The report does not say who made it, so the reporter "
                    "cannot be the contact (§7.1)."
                )
            )
        if not (case.raw_phone.strip() or case.raw_email.strip()):
            raise ValidationError(
                _(
                    "The report carries neither a phone number nor an "
                    "e-mail address for its reporter (§7.1)."
                )
            )

    previous = case.contact_role
    unchanged = (
        previous == role
        and case.contact_name == name
        and case.contact_phone == phone
        and case.contact_email == email
    )
    if unchanged:
        return case

    case.contact_role = role
    case.contact_name = name
    case.contact_phone = phone
    case.contact_email = email
    case.save(
        update_fields=[
            "contact_role",
            "contact_name",
            "contact_phone",
            "contact_email",
        ]
    )

    label = case.get_contact_role_display()
    who = case.contact_details()[0] or "-"
    if previous:
        text = "§7.1: contact person changed to %s (%s)" % (label, who)
    else:
        text = "§7.1: contact person recorded as %s (%s)" % (label, who)
    if note:
        text += " - %s" % note
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text + ".",
    )
    return case


def record_report_location(case: Case, location: str, user, *, note: str = "") -> Case:
    """
    §7.3: where the asset is when the fault is reported.

    ``Case.location`` has been written by both creation paths since they
    were written and read only for display - the case-file card, the
    storage intake sheet, the staff notification. Nothing asked for it,
    which is why a report could reach the scheduling conversation
    without anyone knowing where the asset was.

    Re-recording is allowed and logged: an asset moves, and a report taken
    as "Customer site" that turns out to be a private yard in
    another town changes who drives where.
    """
    location = (location or "").strip()
    if not location:
        raise ValidationError(
            _("Record where the asset is (§7.3).")
        )

    previous = case.location
    if previous == location:
        return case

    case.location = location
    case.save(update_fields=["location"])

    if previous:
        text = "§7.3: report location changed from '%s' to '%s'" % (
            previous,
            location,
        )
    else:
        text = "§7.3: report location recorded as '%s'" % location
    if note:
        text += " - %s" % note
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text + ".",
    )
    return case


def record_client_assessment(
    case: Case,
    user,
    *,
    assessment: str = "",
    gave_no_assessment: bool = False,
    note: str = "",
) -> Case:
    """
    SVC-01 / procedure §7.4: record the client's own assessment of what
    is wrong - the second half of "symptom description and the client's
    assessment of the fault".

    It is deliberately separate from ``symptoms_description``. What the
    asset does is an observation anyone can repeat; what the owner
    thinks is broken is a hypothesis, and it is worth keeping distinct
    precisely so that a diagnosis agreeing or disagreeing with it can be
    seen. Merging the two loses the disagreement.

    Exactly one of the two answers is recorded: the text, or the
    explicit "the client had no view". Both together contradict each
    other; neither is not an answer at all. Re-recording is allowed and
    logged as a change - a client who calls back with a theory is
    ordinary, and the technician about to open the engine should see
    that it arrived late.
    """
    if case.case_type != CaseType.SERVICE:
        raise ValidationError(
            _(
                "What the client thinks is wrong is a service question - "
                "case #%(id)s is a %(kind)s case and reports no fault "
                "(SVC-01)."
            ) % {"id": case.pk, "kind": case.get_case_type_display()}
        )

    assessment = (assessment or "").strip()
    if assessment and gave_no_assessment:
        raise ValidationError(
            _(
                "Either write down what the client thinks is wrong, or "
                "record that they had no view - not both (SVC-01)."
            )
        )
    if not assessment and not gave_no_assessment:
        raise ValidationError(
            _(
                "Record what the client thinks is wrong, or tick that they "
                "had no view (SVC-01)."
            )
        )

    previous = case.client_diagnosis
    previously_none = case.client_gave_no_assessment
    if previous == assessment and previously_none == gave_no_assessment:
        return case

    case.client_diagnosis = assessment
    case.client_gave_no_assessment = gave_no_assessment
    case.save(update_fields=["client_diagnosis", "client_gave_no_assessment"])

    if gave_no_assessment:
        answer = "the client had no view of what is wrong"
    else:
        answer = "'%s'" % assessment
    had_one = bool(previous) or previously_none
    verb = "changed to" if had_one else "recorded as"
    text = "SVC-01: the client's assessment %s %s" % (verb, answer)
    if note:
        text += " - %s" % note
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text + ".",
    )
    return case


def record_work_location_consult(
    case: Case, work_location: str, user, *, note: str = "", consulted=None
) -> Case:
    """
    SVC-03: record the outcome of the pre-arrival consult - whether the
    job can be done on site, in the field, or in the service
    centre - and log that the consult happened.

    The procedure calls this a *may*, so nothing forces it and nothing
    blocks on it; what it changes is the shape of the rest of the case
    (see ``Case.requires_asset_intake``). Re-recording is allowed -
    staff correct a wrong answer - except into an on-site answer once a
    intake record has been signed, because that receipt is a signed record
    that the asset WAS taken in, and the two cannot both be true.

    ``consulted`` is the technician who was asked; it is kept in the log
    text rather than in a column, because what drives the workflow is
    the answer, not who gave it.
    """
    from .models import WorkLocation

    # SVC-03 is a question about a service job: can this repair be done
    # on site, in the field, or does the asset come in? A
    # transported or stored asset is by definition taken in, and an
    # on-site answer on such a case switched off ``requires_asset_intake``
    # - which took the whole TRP-09 handover requirement with it, because
    # the closing checklist, the comparison card and four guide steps all
    # hang off that flag. The endpoint was live on every case type even
    # though the card is only drawn for SERVICE.
    if case.case_type != CaseType.SERVICE:
        raise ValidationError(
            _(
                "Where the work is carried out is a service question - "
                "case #%(id)s is a %(kind)s case, and its asset is taken "
                "in either way (SVC-03)."
            ) % {"id": case.pk, "kind": case.get_case_type_display()}
        )

    if work_location not in WorkLocation.values:
        raise ValidationError(
            _("Choose where the work is carried out (SVC-03).")
        )

    goes_on_site = work_location in ON_SITE_WORK_LOCATIONS
    if goes_on_site:
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        if IntakeRecord.objects.filter(
            case=case, signed_at__isnull=False
        ).exists():
            raise ValidationError(
                _(
                    "Case #%(id)s already has a signed intake record - the "
                    "asset was received, so the work cannot be recorded as "
                    "done where it lies (SVC-03/SVC-06)."
                ) % {"id": case.pk}
            )

    case.work_location = work_location
    case.save(update_fields=["work_location"])

    parts = [
        "SVC-03: work location recorded as '%s'"
        % case.get_work_location_display()
    ]
    if consulted is not None:
        parts.append("technician consulted: '%s'" % consulted.get_username())
    if (note or "").strip():
        parts.append(note.strip())
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.NOTE,
        text="; ".join(parts) + ".",
    )
    # SVC-04 is settled downstream of this answer, so a method agreed
    # before the consult can be contradicted by it.
    _clear_delivery_method_for_on_site(case, user)
    return case


# Registry: target status code -> list of guard callables
# (case, from_status) -> None.
def _guard_diagnostics(case: Case, from_status: Status) -> None:
    """
    SVC-01 / procedure §7.4: the fault report is complete before anyone
    starts diagnosing it.

    Both halves: what the asset does, and what its owner thinks is
    wrong. The symptom description has been mandatory on the portal form
    and on the staff create form since they were written - which means
    it was mandatory for people who opened those forms, and for nobody
    else. This is the gate.

    Diagnostics is the moment the data is needed rather than the moment
    it is easiest to collect: the technician about to open the engine is
    the reader. Staff are prompted for it far earlier - an advisory
    guide step sits on ``new_report``, while the client is still on the
    phone - but the refusal waits until it actually matters.
    """
    if case.case_type != CaseType.SERVICE:
        return
    if not case.symptoms_description.strip():
        raise GuardCheckFailed(
            _(
                "Cannot start diagnostics on case #%(id)s: nothing "
                "describes what the asset is doing (SVC-01)."
            ) % {"id": case.pk}
        )
    if not case.client_assessment_recorded:
        raise GuardCheckFailed(
            _(
                "Cannot start diagnostics on case #%(id)s: the client has "
                "not been asked what they think is wrong - record their "
                "assessment, or that they had none (SVC-01)."
            ) % {"id": case.pk}
        )


GUARDS = {
    "intake_scheduled": [_guard_intake_scheduled],
    "diagnostics": [_guard_diagnostics],
    "quote_sent": [_guard_quote_sent],
    "advance_paid": [_guard_advance_paid],
    "asset_received": [_guard_asset_received],
    "awaiting_approval": [_guard_diagnostics_recorded, _guard_new_client_inspection],
    "awaiting_part": [_guard_awaiting_part],
    "in_progress": [
        _guard_diagnostics_recorded,
        _guard_quote_approved,
        _guard_stock_shortcut,
    ],
    "quality_check": [_guard_extra_work_approved],
    "ready_for_handover": [_guard_ready_for_handover],
    "departure_scheduled": [_guard_departure_scheduled],
    "in_transit": [_guard_in_transit],
    "delivered": [_guard_delivered],
    "contracted": [_guard_contracted],
    "estimate_sent": [_guard_estimate_sent],
    "in_storage": [_guard_in_storage],
    "closed": [_guard_closed],
}

#: Statuses that record a physical event - the asset set down on its spot,
#: declared ready to collect, driven off. ``auto_advance_case`` never
#: enters them: records can show the event is possible, but only a person
#: can say it happened (QA S-H1, T-H1).
MANUAL_ONLY_STATUSES = frozenset({"in_storage", "ready_for_pickup", "in_transit"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def serialised_per_case(func):
    """Run ``func(case, ...)`` in a transaction holding the case row lock.

    For the check-then-insert services ("one unsigned intake record per
    case", "one derived advance"): without the lock, parallel submits all
    passed the check before any of them inserted (QA SV-M2, T-M4).
    """
    import functools

    @functools.wraps(func)
    def wrapper(case, *args, **kwargs):
        with transaction.atomic():
            list(
                Case.objects.select_for_update()
                .filter(pk=case.pk)
                .values_list("pk", flat=True)
            )
            return func(case, *args, **kwargs)

    return wrapper


def _role_allows(transition: StatusTransition, user) -> bool:
    """
    STM-06 / ACT-01: may ``user`` take ``transition``?

    ``allowed_roles`` empty means any staff role. Superusers bypass the
    check. ``user=None`` means an internal/system caller (e.g. tests or
    services acting on behalf of the system) and is unrestricted.
    """
    allowed_roles = transition.allowed_roles or []
    if not allowed_roles:
        return True
    if user is None or user.is_superuser:
        return True
    return user.role in allowed_roles


def transition_case(case: Case, to_status_code: str, user, note: str = "") -> Case:
    """
    Move ``case`` to the status identified by ``to_status_code``.

    Validates the transition against the data-driven workflow, checks the
    user's role against the transition's ``allowed_roles`` (STM-06), runs
    guard checks, records a ``StatusHistory`` row and saves the case. The
    client notification is scheduled via ``transaction.on_commit`` so a
    rolled-back transition can never send an e-mail (NTF-03). Raises
    ``InvalidTransition``, ``RoleNotAllowed`` or ``GuardCheckFailed``.
    """
    with transaction.atomic():
        # Serialise per case, and judge the move from the status the row
        # has NOW: two concurrent clicks used to both pass from the same
        # stale status and write the transition twice (QA T-M4).
        # The whole row is re-read under the lock, not only the status:
        # guards judge settlement, contact and delivery fields too, and
        # the caller's snapshot may predate another writer's change.
        Case.objects.select_for_update().values_list("pk", flat=True).get(pk=case.pk)
        case.refresh_from_db()
        try:
            to_status = Status.objects.get(
                case_type=case.case_type, code=to_status_code
            )
        except Status.DoesNotExist:
            raise InvalidTransition(
                _("No status with code '%(code)s' exists for case type '%(type)s'.")
                % {"code": to_status_code, "type": case.case_type}
            )

        old_status = case.status
        transition = StatusTransition.objects.filter(
            from_status=old_status, to_status=to_status
        ).first()
        if transition is None:
            raise InvalidTransition(
                _(
                    "Transition from '%(from)s' to '%(to)s' is not allowed "
                    "for case type '%(type)s'."
                ) % {
                    "from": old_status.code,
                    "to": to_status.code,
                    "type": case.case_type,
                }
            )

        if not _role_allows(transition, user):
            raise RoleNotAllowed(
                _(
                    "Cannot move case #%(id)s from '%(from)s' to '%(to)s': "
                    "role '%(role)s' is not allowed to take this transition "
                    "(allowed: %(allowed)s)."
                ) % {
                    "id": case.pk,
                    "from": old_status.code,
                    "to": to_status.code,
                    "role": user.role or _("(none)"),
                    "allowed": ", ".join(transition.allowed_roles),
                }
            )

        for guard in GUARDS.get(to_status.code, []):
            guard(case, old_status)

        case.status = to_status
        StatusHistory.objects.create(
            case=case,
            from_status=old_status,
            to_status=to_status,
            changed_by=user,
            note=note,
        )
        # Only the status: a bare save() wrote every column back from the
        # snapshot and silently undid concurrent edits to other fields.
        case.save(update_fields=["status", "updated_at"])

        # SVC-20 (procedure §4.5): "The work order **and** the service
        # ticket are closed..." - both documents close, and they close
        # together. Inside the same atomic block as the status change so
        # a rolled-back close cannot leave an order stamped closed on an
        # open case. Not in the guard: guards answer a question, they do
        # not change anything (INV-01).
        if to_status.code == "closed":
            work_order = getattr(case, "work_order", None)
            if work_order is not None:
                # Imported lazily; apps.service resolves this module the
                # same way, and a module-level import would be circular.
                from apps.service.services import close_work_order

                close_work_order(work_order, user)

            # A closed storage case has handed the asset back: its ground
            # is free. It kept blocking placements and was still drawn on
            # the plan and in the fit-check (QA S-M5).
            agreement = getattr(case, "storage_agreement", None)
            if agreement is not None:
                released = [str(p) for p in agreement.positions.all()]
                if released:
                    agreement.positions.all().delete()
                    ActivityLog.objects.create(
                        case=case,
                        author=user,
                        kind=ActivityLog.Kind.SYSTEM,
                        text=(
                            "WNT-05: case closed - storage position(s) "
                            "released: %s." % ", ".join(released)
                        ),
                    )

        def _notify():
            # Imported lazily: apps.notifications is developed concurrently.
            from apps.notifications.services import notify_case_transition

            notify_case_transition(case, old_status, to_status)

        transaction.on_commit(_notify)
        return case


def available_transitions(case: Case, user) -> list:
    """
    Return the list of ``Status`` objects reachable from the case's
    current status via ``StatusTransition`` rows, filtered by the
    transition's ``allowed_roles`` for ``user`` (STM-06 / ACT-01).

    Transitions with an empty ``allowed_roles`` list are open to any
    staff role. Superusers see everything; ``user=None`` (internal or
    system callers) is unrestricted.
    """
    transitions = (
        StatusTransition.objects.filter(from_status=case.status)
        .select_related("to_status")
        .order_by("to_status__sort_order")
    )
    return [
        transition.to_status
        for transition in transitions
        if _role_allows(transition, user)
    ]


def auto_advance_case(case: Case, user, *, max_hops: int = 12) -> list:
    """
    Advance ``case`` automatically along *linear* edges so staff never
    have to click a transition that involves no decision (the wizard stays
    a read-only guide).

    While the current status has exactly ONE outgoing transition that the
    user's role may take (STM-06) and whose guards all pass, take it. The
    chain stops at the first status that is a genuine human decision point:

    * a branch or dead-end (0 or >1 outgoing edges) - a human must choose;
    * an edge the user's role may not take - the approval/authorization is
      left to the role that owns it (a manager approval never auto-fires
      for a technician's edit);
    * an unmet guard - the precondition simply is not done yet;
    * a status in ``MANUAL_ONLY_STATUSES`` - a physical event;
    * a terminal target - closing a case stays an explicit act (it is tied
      to financial settlement, INV-07).

    Runs each hop through :func:`transition_case`, so guards, role checks
    and the ``StatusHistory``/notification side-effects are identical to a
    manual click. Never raises: a blocked hop just ends the chain. Returns
    the list of ``Status`` objects the case moved into (possibly empty).
    """
    advanced = []
    for _hop in range(max_hops):
        case.refresh_from_db()
        if case.status.is_terminal:
            break
        edges = list(
            StatusTransition.objects.filter(
                from_status=case.status
            ).select_related("to_status")
        )
        if len(edges) != 1:
            break  # branch or dead-end: leave the choice to a human
        edge = edges[0]
        if edge.to_status.is_terminal:
            break  # never auto-close (INV-07)
        if edge.to_status.code in MANUAL_ONLY_STATUSES:
            break  # a physical event: a person says it happened
        if not _role_allows(edge, user):
            break  # authorization/approval gate owned by another role
        try:
            transition_case(
                case,
                edge.to_status.code,
                user,
                note=str(_("Automatic: single next step, preconditions met.")),
            )
        except CaseWorkflowError:
            break  # unmet guard (or any refusal): precondition not ready yet
        advanced.append(case.status)
    return advanced


# ---------------------------------------------------------------------------
# Case media (attachments spread across the case and its child records)
# ---------------------------------------------------------------------------

def attachment_targets_for_case(case: Case) -> list:
    """Every object under ``case`` that can carry attachments.

    A work object's files do not all live on the ``Case`` row: the
    client-approval scan sits on the work order (SVC-05), the handover
    approval on the handover record (SVC-18) and the delivery protocol on
    the transport delivery confirmation (TRP-08). Gathering the targets in
    one place lets ``case_attachments`` build the full media set for the
    work object. Reverse one-to-one relations raise on absence, but their
    exception subclasses ``AttributeError``, so ``getattr(..., None)`` is
    safe.
    """
    targets = [case]
    targets.extend(case.intake_records.all())
    targets.extend(case.handover_records.all())
    work_order = getattr(case, "work_order", None)
    if work_order is not None:
        targets.append(work_order)
    transport_order = getattr(case, "transport_order", None)
    if transport_order is not None:
        delivery = getattr(transport_order, "delivery_confirmation", None)
        if delivery is not None:
            targets.append(delivery)
    return targets


def case_attachments(case: Case):
    """All attachments belonging to ``case`` and its child records, newest
    first - the full media set for one work object (photos, videos and
    document scans), regardless of which record each file hangs off."""
    from apps.attachments.services import attachments_for_objects

    return attachments_for_objects(
        attachment_targets_for_case(case)
    ).order_by("-created_at")


def asset_attachments(asset):
    """All attachments across every case of ``asset`` and their child
    records, newest first - the asset's whole media history in one gallery,
    regardless of which case or record each file hangs off."""
    from apps.attachments.services import attachments_for_objects

    # Four fixed queries for the targets instead of ~5 per case: an asset
    # with twenty cases used to cost ~100 queries before the union.
    cases = asset.cases.prefetch_related(
        "intake_records",
        "handover_records",
        "work_order",
        "transport_order__delivery_confirmation",
    )
    targets = []
    for case in cases:
        targets.extend(attachment_targets_for_case(case))
    return attachments_for_objects(targets).order_by("-created_at")


def closed_case_message(case: Case):
    """INV-15: the refusal text when ``case`` is closed, else ``None``.
    Every case-scoped write has to ask - the ones that did not were the
    QA review's root cause 2. Shared by the views and the services that
    write to a case outside ``transition_case``."""
    if not case.status.is_terminal:
        return None
    return _(
        "Case #%(id)s is closed ('%(status)s'); it takes no further "
        "changes. Attachments may still be added to the case file."
    ) % {"id": case.pk, "status": case.status.name}


def add_activity_entry(case: Case, user, kind: str, text: str) -> ActivityLog:
    """
    GEN-02: append a staff-authored ``ActivityLog`` entry to ``case`` -
    a note, or a phone agreement affecting price, scope or deadline,
    documented after the fact. Append-only; refused on a closed case
    (INV-15), which the inline form used to decide for itself.
    """
    refusal = closed_case_message(case)
    if refusal:
        raise ValidationError(refusal)
    return ActivityLog.objects.create(case=case, author=user, kind=kind, text=text)


# Roles that may flip the financial-settlement flag: the same roles that
# own every edge into ``closed`` (seed_workflow), since the flag is half
# of that guard (SVC-19 / INV-07).
SETTLEMENT_ROLES = ("MANAGER", "DIRECTOR")


def set_financial_settlement(case: Case, user, settled: bool, note: str = "") -> Case:
    """
    SVC-19 / INV-07: mark ``case`` as financially settled (or revert a
    mistaken flip). Restricted to MANAGER/DIRECTOR (superusers bypass) -
    the roles allowed to close a case; both directions are logged as a
    SYSTEM activity entry so the audit trail survives corrections.

    Does not touch ``case.status`` (INV-01); closing still runs through
    ``transition_case()`` and its guard.
    """
    if user is not None and not user.is_superuser and user.role not in SETTLEMENT_ROLES:
        raise ValidationError(
            _(
                "Role '%(role)s' may not change financial settlement on case "
                "#%(id)s (allowed: %(allowed)s)."
            ) % {
                "role": user.role or _("(none)"),
                "id": case.pk,
                "allowed": ", ".join(SETTLEMENT_ROLES),
            }
        )
    if case.financially_settled == settled:
        raise ValidationError(
            _("Case #%(id)s is already marked as %(state)s.") % {
                "id": case.pk,
                "state": _("settled") if settled else _("not settled"),
            }
        )
    case.financially_settled = settled
    case.save(update_fields=["financially_settled"])
    text = "SVC-19: case marked as financially %s." % (
        "settled" if settled else "NOT settled"
    )
    if note:
        text += " Note: %s" % note
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=text,
    )
    return case


# Initial status code per case type: SERVICE starts as a fresh report,
# TRANSPORT and STORAGE start as an inquiry (seed_workflow).
INITIAL_STATUS_CODES = {
    CaseType.SERVICE: "new_report",
    CaseType.TRANSPORT: "inquiry",
    CaseType.STORAGE: "inquiry",
}


def initial_status_for(case_type) -> Status:
    """Return the initial ``Status`` for a new case of ``case_type``.

    Statuses are seeded by a management command; fail loudly if the
    expected one is missing so the misconfiguration is obvious.
    """
    from django.core.exceptions import ImproperlyConfigured

    code = INITIAL_STATUS_CODES[CaseType(case_type)]
    try:
        return Status.objects.get(case_type=case_type, code=code)
    except Status.DoesNotExist as exc:
        raise ImproperlyConfigured(
            'Initial %s status with code "%s" does not exist. '
            "Run the status seeding management command first." % (case_type, code)
        ) from exc


def create_staff_case(
    *,
    case_type,
    report_channel,
    contact_name,
    phone="",
    email="",
    asset_info="",
    location="",
    symptoms_description="",
    client_diagnosis="",
    user=None,
) -> Case:
    """
    Create a case from a report received by staff (phone/e-mail/walk-in).

    The internal counterpart of ``create_public_report``: the caller's
    details land in the ``raw_*`` fields (matched to real client/asset
    records at triage, GEN-03) and the initial status is derived from the
    case type - staff never pick it. The client confirmation (NTF-04) is
    scheduled via ``transaction.on_commit`` so a rolled-back creation
    never notifies anyone.
    """
    with transaction.atomic():
        case = Case.objects.create(
            case_type=case_type,
            report_channel=report_channel,
            status=initial_status_for(case_type),
            raw_contact_name=contact_name,
            raw_phone=phone or "",
            raw_email=email or "",
            raw_asset_info=asset_info or "",
            location=location or "",
            symptoms_description=symptoms_description or "",
            client_diagnosis=client_diagnosis or "",
        )
        ActivityLog.objects.create(
            case=case,
            author=user,
            kind=ActivityLog.Kind.SYSTEM,
            text="Case created in backoffice (channel: %s)." % report_channel,
        )

        def _notify():
            # Imported lazily: apps.notifications is developed concurrently.
            from apps.notifications.services import notify_case_created

            notify_case_created(case)

        transaction.on_commit(_notify)
        return case
