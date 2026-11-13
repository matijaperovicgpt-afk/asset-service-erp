"""
Tests for the case workflow state machine and its guards.

Fixtures are built inline (statuses + transitions created directly) so
the tests are self-contained and do not depend on the seed command.
Cross-app models (service, intake, ...) are resolved via ``apps.get_model``
because those apps are developed in parallel.

"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from .exceptions import (
    AppendOnlyViolation,
    GuardCheckFailed,
    InvalidTransition,
    RoleNotAllowed,
)
from .models import (
    ActivityLog,
    Case,
    CaseType,
    ContactRole,
    ReportChannel,
    Status,
    StatusHistory,
    StatusTransition,
)
from django.core.exceptions import ValidationError as DjangoValidationError

from .services import (
    auto_advance_case,
    available_transitions,
    set_financial_settlement,
    transition_case,
)


def build_workflow(case_type, codes, extra_edges=()):
    """
    Create ``Status`` rows for ``codes`` (linear chain in order) plus
    ``extra_edges`` [(from_code, to_code)], mirroring the seed command.
    Returns a dict code -> Status.
    """
    statuses = {}
    for sort_order, code in enumerate(codes):
        statuses[code] = Status.objects.create(
            case_type=case_type,
            code=code,
            name=code,
            client_visible_name=code,
            sort_order=sort_order,
            is_terminal=(sort_order == len(codes) - 1),
        )
    chain = list(zip(codes, codes[1:]))
    for from_code, to_code in chain + list(extra_edges):
        StatusTransition.objects.create(
            from_status=statuses[from_code], to_status=statuses[to_code]
        )
    return statuses


SERVICE_CODES = [
    "new_report",
    "intake_scheduled",
    "asset_received",
    "diagnostics",
    "awaiting_approval",
    "awaiting_part",
    "in_progress",
    "quality_check",
    "ready_for_handover",
    "closed",
]
SERVICE_EXTRA_EDGES = [
    ("diagnostics", "in_progress"),
    ("awaiting_approval", "in_progress"),
    ("in_progress", "awaiting_approval"),
    ("quality_check", "in_progress"),
]


def build_service_workflow():
    return build_workflow(CaseType.SERVICE, SERVICE_CODES, SERVICE_EXTRA_EDGES)




def _service_type(name="Storage"):
    """WNT-02 (§5.2): the contracted guard wants at least one selected
    service, so the fixtures that expect to reach ``contracted`` need one
    in the catalogue."""
    StorageServiceType = django_apps.get_model(
        "storage", "StorageServiceType"
    )
    service_type, _created = StorageServiceType.objects.get_or_create(name=name)
    return service_type

class WorkflowTestBase(TestCase):
    """Shared helpers: a staff user and a patched transition wrapper."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="tester", password="demo12345", is_staff=True
        )

    def _transition(self, case, to_code, note=""):
        # Notifications run via transaction.on_commit; patch them out so
        # tests using captureOnCommitCallbacks stay side-effect free.
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                return transition_case(case, to_code, self.user, note=note)

    def _make_case(self, statuses, code, case_type=CaseType.SERVICE, **kwargs):
        kwargs.setdefault("report_channel", ReportChannel.PHONE)
        kwargs.setdefault("raw_contact_name", "Test Contact")
        # §7.1: a designation only counts while it still reaches
        # somebody, so the fixture's reporter carries a number. A test
        # that needs an unreachable case passes raw_phone="" explicitly -
        # ``setdefault`` leaves it alone.
        kwargs.setdefault("raw_phone", "+44 67 111 222")
        # Who we contact, which is what staff would actually click: a
        # case with a client linked is contacted through the owner, one
        # still carrying only raw submission data through its reporter.
        # A client with no contact data of its own cannot be the answer.
        client = kwargs.get("client")
        client_reachable = client is not None and (client.phone or client.email)
        kwargs.setdefault(
            "contact_role",
            ContactRole.OWNER if client_reachable else ContactRole.REPORTER,
        )
        # §7.3: a fault report records where the asset is, and the
        # intake_scheduled guard now asks for it.
        kwargs.setdefault("location", "Customer site")
        return Case.objects.create(
            case_type=case_type, status=statuses[code], **kwargs
        )

    def _add_intake_appointment(self, case, delivery_method="CLIENT_SHIPS"):
        """SVC-04: the date and the way the asset gets here are agreed in
        the same conversation, so the fixture records both."""
        Appointment = django_apps.get_model("scheduling", "Appointment")
        appointment = Appointment.objects.create(
            case=case,
            kind=Appointment.Kind.INTAKE,
            scheduled_for=timezone.now() + timedelta(days=1),
        )
        if delivery_method and not case.works_on_site:
            case.delivery_method = delivery_method
            case.save(update_fields=["delivery_method"])
        return appointment


class WorkflowTestCase(WorkflowTestBase):
    """Core state-machine behaviour (STM-02/03/04)."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "new_report")

    # 1. Allowed transition succeeds and writes StatusHistory (STM-03).
    def test_allowed_transition_succeeds_and_writes_history(self):
        self._add_intake_appointment(self.case)
        case = self._transition(self.case, "intake_scheduled", note="Scheduled for Monday")
        self.assertEqual(case.status, self.statuses["intake_scheduled"])
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["intake_scheduled"])

        history = self.case.status_history.get()
        self.assertEqual(history.from_status, self.statuses["new_report"])
        self.assertEqual(history.to_status, self.statuses["intake_scheduled"])
        self.assertEqual(history.changed_by, self.user)
        self.assertEqual(history.note, "Scheduled for Monday")

    # 2. Disallowed transition raises InvalidTransition.
    def test_disallowed_transition_raises(self):
        with self.assertRaises(InvalidTransition):
            self._transition(self.case, "closed")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["new_report"])
        self.assertEqual(StatusHistory.objects.count(), 0)

    def test_available_transitions(self):
        statuses = available_transitions(self.case, self.user)
        self.assertEqual(statuses, [self.statuses["intake_scheduled"]])

    # SVC-04: intake appointment required before intake_scheduled.
    def test_intake_scheduled_guard_requires_appointment(self):
        with self.assertRaisesMessage(GuardCheckFailed, "intake appointment"):
            self._transition(self.case, "intake_scheduled")
        self._add_intake_appointment(self.case)
        case = self._transition(self.case, "intake_scheduled")
        self.assertEqual(case.status.code, "intake_scheduled")

    # SVC-05: technician-signed WorkOrder + client-approval scan + signed
    # IntakeRecord before asset_received.
    def test_asset_received_guard(self):
        self.case.status = self.statuses["intake_scheduled"]
        self.case.save()
        # §7.2: the ticket has to name its asset before it is received,
        # whichever route it took to the status.
        from apps.clients.tests import link_identified_asset

        link_identified_asset(self.case)

        with self.assertRaises(GuardCheckFailed):
            self._transition(self.case, "asset_received")

        WorkOrder = django_apps.get_model("service", "WorkOrder")
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")

        work_order = WorkOrder.objects.create(
            case=self.case,
            is_preliminary=False,
            signed_by_technician=self.user,
            signed_at=timezone.now(),
        )
        IntakeRecord.objects.create(
            case=self.case,
            received_by=self.user,
            mount_status="OFF_MOUNT",
            signed_by=self.user,
            signed_at=timezone.now(),
        )
        # Signed work order + intake record without the client-approval
        # scan is not enough.
        with self.assertRaisesMessage(GuardCheckFailed, "client-approval"):
            self._transition(self.case, "asset_received")

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            from apps.service.services import attach_client_approval

            attach_client_approval(
                work_order,
                SimpleUploadedFile("work-order.pdf", b"%PDF-1.4 scan"),
                self.user,
            )
            case = self._transition(self.case, "asset_received")
        self.assertEqual(case.status.code, "asset_received")

    # SVC-19 / INV-07: signed HandoverRecord + financially_settled before closed.
    def test_closed_guard(self):
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()

        with self.assertRaises(GuardCheckFailed):
            self._transition(self.case, "closed")

        HandoverRecord = django_apps.get_model("intake", "HandoverRecord")
        HandoverRecord.objects.create(
            case=self.case,
            handed_over_by=self.user,
            received_by_name="Test Contact",
            signed_at=timezone.now(),
        )
        # Handover alone is not enough: still not financially settled.
        with self.assertRaises(GuardCheckFailed):
            self._transition(self.case, "closed")

        self.case.financially_settled = True
        self.case.save()
        case = self._transition(self.case, "closed")
        self.assertEqual(case.status.code, "closed")


class NewClientInspectionGuardTests(WorkflowTestBase):
    """SVC-08: a first-time client cannot leave diagnostics for the approval
    phase without a signed full inspection; returning clients and the
    diagnostics->in_progress consumable shortcut are unaffected."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "diagnostics")
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False, signed_at=timezone.now()
        )
        # Satisfy the SVC-09 diagnostics guard so we isolate SVC-08.
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        Diagnostic.objects.create(
            work_order=self.work_order, issue="x", servicing_work="y",
            parts_needed="z", estimated_parts_price=Decimal("10"),
            estimated_completion_date=timezone.now().date(),
        )
        InspectionPoint = django_apps.get_model("service", "InspectionPoint")
        InspectionPoint.objects.create(system="STRUCTURE", label="Structure", sort_order=0)

    def _sign_inspection(self):
        from apps.service.services import (
            record_inspection_result, sign_inspection_record, start_inspection,
        )
        record = start_inspection(self.work_order, self.user)
        for item in record.items.all():
            record_inspection_result(item, self.user, status="OK")
        return sign_inspection_record(record, self.user)

    def test_new_client_blocked_until_inspection_signed(self):
        with self.assertRaisesMessage(GuardCheckFailed, "full inspection"):
            self._transition(self.case, "awaiting_approval")
        self._sign_inspection()
        case = self._transition(self.case, "awaiting_approval")
        self.assertEqual(case.status.code, "awaiting_approval")

    def test_in_progress_shortcut_needs_no_inspection(self):
        # SVC-10: the consumable shortcut issues no quote, so it needs no
        # inspection even for a first-time client. It does need the cost to
        # be inside the service's own limit (SVC-10 guards that separately),
        # so publish a technician limit that covers this diagnostic.
        from apps.accounts.models import ApprovalLimit, Role

        ApprovalLimit.objects.create(
            role=Role.TECHNICIAN, max_amount=Decimal("500")
        )
        # SVC-10 also wants the parts identified; this fixture's diagnostic
        # described them in prose, which now costs the shortcut.
        Part = django_apps.get_model("parts", "Part")
        DiagnosticPart = django_apps.get_model("service", "DiagnosticPart")
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        diagnostic = Diagnostic.objects.get(work_order=self.work_order)
        diagnostic.parts_needed = ""
        diagnostic.save()
        DiagnosticPart.objects.create(
            diagnostic=diagnostic,
            part=Part.objects.create(
                code="OIL-8", name="Oil filter", is_consumable=True,
                stock_qty=6,
            ),
            quantity=1,
        )
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")

    def test_in_progress_shortcut_refused_above_the_service_limit(self):
        """SVC-10: without a published limit the service has no authority
        to self-approve, and the wizard's promise is now a rule."""
        with self.assertRaisesMessage(GuardCheckFailed, "SVC-10"):
            self._transition(self.case, "in_progress")

    def test_returning_client_not_blocked(self):
        Client = django_apps.get_model("clients", "Client")
        client = Client.objects.create(name="Ana")
        self.case.client = client
        self.case.save()
        Case.objects.create(
            case_type=CaseType.SERVICE, report_channel=ReportChannel.PHONE,
            raw_contact_name="Ana", status=self.statuses["closed"], client=client,
        )
        case = self._transition(self.case, "awaiting_approval")
        self.assertEqual(case.status.code, "awaiting_approval")


class ServiceFlowGuardTests(WorkflowTestBase):
    """Guards on the SERVICE flow: SVC-09/10/13/14/16, INV-04/05."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "diagnostics")
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False, signed_at=timezone.now()
        )
        ApprovalLimit = django_apps.get_model("accounts", "ApprovalLimit")
        for role, amount in [
            ("TECHNICIAN", "100"),
            ("MANAGER", "1000"),
            ("DIRECTOR", "100000"),
        ]:
            ApprovalLimit.objects.create(role=role, max_amount=Decimal(amount))
        # These guards are about SVC-09/10/13/14/16, not SVC-08. Give the
        # case a *returning* client - one with an earlier closed case - so
        # the new-client inspection guard stays out of the way; SVC-08 has
        # its own suite in ``NewClientInspectionGuardTests``.
        Client = django_apps.get_model("clients", "Client")
        client = Client.objects.create(name="Returning Client")
        self.case.client = client
        self.case.save(update_fields=["client"])
        Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Returning Client",
            status=self.statuses["closed"],
            client=client,
        )

    def _add_diagnostic(self, catalogue_parts=None, **overrides):
        """A recorded diagnostic. ``catalogue_parts`` is [(part, qty)] for
        the SVC-10 structured half; the free-text half stands in for parts
        the catalogue has no row for, and costs the case the shortcut."""
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        DiagnosticPart = django_apps.get_model("service", "DiagnosticPart")
        fields = {
            "work_order": self.work_order,
            "issue": "Impeller worn out.",
            "servicing_work": "Replace impeller, flush cooling circuit.",
            "parts_needed": "Impeller kit 500104.",
            "estimated_parts_price": Decimal("45.00"),
            "estimated_completion_date": timezone.now().date(),
        }
        fields.update(overrides)
        diagnostic = Diagnostic.objects.create(**fields)
        for part, quantity in catalogue_parts or []:
            DiagnosticPart.objects.create(
                diagnostic=diagnostic, part=part, quantity=quantity
            )
        return diagnostic

    def _stock_part(self, code="IMP-1", quantity=5):
        """A consumable the store actually holds - the SVC-10 happy path."""
        Part = django_apps.get_model("parts", "Part")
        return Part.objects.create(
            code=code, name="Impeller kit", is_consumable=True,
            stock_qty=quantity,
        )

    def _add_quote(self, amount, status=None):
        Quote = django_apps.get_model("quotes", "Quote")
        return Quote.objects.create(
            case=self.case,
            description="Repair",
            amount=Decimal(amount),
            status=status or Quote.Status.ACCEPTED,
        )

    def _approve(self, quote, level):
        Approval = django_apps.get_model("quotes", "Approval")
        return Approval.objects.create(
            quote=quote,
            level=level,
            decision=Approval.Decision.APPROVED,
            decided_at=timezone.now(),
        )

    # SVC-09: no leaving diagnostics without recorded findings.
    def test_leaving_diagnostics_requires_diagnostic(self):
        with self.assertRaisesMessage(GuardCheckFailed, "diagnostic"):
            self._transition(self.case, "awaiting_approval")
        with self.assertRaisesMessage(GuardCheckFailed, "diagnostic"):
            self._transition(self.case, "in_progress")

        self._add_diagnostic()
        case = self._transition(self.case, "awaiting_approval")
        self.assertEqual(case.status.code, "awaiting_approval")

    # SVC-09: all four pieces are mandatory - a partial diagnostic (legacy
    # row with a blank field) does not open the gate.
    def test_partial_diagnostic_does_not_satisfy_guard(self):
        self._add_diagnostic(parts_needed="")
        with self.assertRaisesMessage(GuardCheckFailed, "parts needed"):
            self._transition(self.case, "awaiting_approval")

        self._add_diagnostic()
        case = self._transition(self.case, "awaiting_approval")
        self.assertEqual(case.status.code, "awaiting_approval")

    # SVC-10: diagnostics -> in_progress shortcut needs no quote approval.
    def test_diagnostics_to_in_progress_needs_no_quote(self):
        # SVC-10: parts picked from the catalogue, nothing left to
        # describe - which is what the shortcut asks for.
        self._add_diagnostic(
            catalogue_parts=[(self._stock_part(), 1)], parts_needed=""
        )
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")

    def test_the_shortcut_is_refused_while_a_part_is_only_described(self):
        """SVC-10: free text means the catalogue has no row for it, and an
        unidentified part cannot be shown to be a consumable in stock."""
        self._add_diagnostic(parts_needed="Impeller kit 500104.")
        with self.assertRaisesMessage(GuardCheckFailed, "does not know"):
            self._transition(self.case, "in_progress")

    def test_the_quote_path_accepts_a_described_part(self):
        """The refusal costs the shortcut, not the diagnosis: the same
        case walks out of diagnostics the normal way."""
        self._add_diagnostic(parts_needed="Impeller kit 500104.")
        case = self._transition(self.case, "awaiting_approval")
        self.assertEqual(case.status.code, "awaiting_approval")

    def test_the_shortcut_is_refused_when_the_store_is_short(self):
        part = self._stock_part(quantity=1)
        self._add_diagnostic(
            catalogue_parts=[(part, 4)], parts_needed=""
        )
        with self.assertRaisesMessage(GuardCheckFailed, "does not hold enough"):
            self._transition(self.case, "in_progress")

    def test_the_shortcut_is_refused_for_a_non_consumable(self):
        Part = django_apps.get_model("parts", "Part")
        part = Part.objects.create(
            code="GBX-9", name="Gearbox", is_consumable=False, stock_qty=3
        )
        self._add_diagnostic(catalogue_parts=[(part, 1)], parts_needed="")
        with self.assertRaisesMessage(GuardCheckFailed, "not a regular consumable"):
            self._transition(self.case, "in_progress")

    # SVC-13 / INV-04: awaiting_approval -> in_progress needs an accepted,
    # fully approved quote.
    def test_awaiting_approval_to_in_progress_requires_approved_quote(self):
        self._add_diagnostic()
        self.case.status = self.statuses["awaiting_approval"]
        self.case.save()

        with self.assertRaisesMessage(GuardCheckFailed, "no quote"):
            self._transition(self.case, "in_progress")

        Quote = django_apps.get_model("quotes", "Quote")
        quote = self._add_quote("500", status=Quote.Status.SENT)
        with self.assertRaisesMessage(GuardCheckFailed, "not accepted"):
            self._transition(self.case, "in_progress")

        quote.status = Quote.Status.ACCEPTED
        quote.save()
        # 500 is above the technician limit (100): CLIENT + MANAGER required.
        with self.assertRaisesMessage(GuardCheckFailed, "missing approval"):
            self._transition(self.case, "in_progress")

        Approval = django_apps.get_model("quotes", "Approval")
        self._approve(quote, Approval.Level.CLIENT)
        with self.assertRaisesMessage(GuardCheckFailed, "manager"):
            self._transition(self.case, "in_progress")

        self._approve(quote, Approval.Level.MANAGER)
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")

    # SVC-14: awaiting_part needs an open PartOrder with expected delivery;
    # INV-05: non-stock part orders must carry an advance payment.
    def test_awaiting_part_guard(self):
        self._add_diagnostic()
        self.case.status = self.statuses["awaiting_approval"]
        self.case.save()

        with self.assertRaisesMessage(GuardCheckFailed, "part order"):
            self._transition(self.case, "awaiting_part")

        Part = django_apps.get_model("parts", "Part")
        PartOrder = django_apps.get_model("parts", "PartOrder")
        stock_part = Part.objects.create(
            code="IMP-1", name="Impeller", is_consumable=True, stock_qty=3
        )
        order = PartOrder.objects.create(case=self.case, part=stock_part)
        # Open order without an expected delivery date is not enough.
        with self.assertRaisesMessage(GuardCheckFailed, "expected delivery"):
            self._transition(self.case, "awaiting_part")

        order.expected_delivery = timezone.localdate() + timedelta(days=5)
        order.save()

        # A non-stock order without a linked advance blocks (INV-05).
        rare_part = Part.objects.create(
            code="GBX-9", name="Gearbox", is_consumable=False, stock_qty=0
        )
        rare_order = PartOrder.objects.create(
            case=self.case,
            part=rare_part,
            expected_delivery=timezone.localdate() + timedelta(days=30),
        )
        with self.assertRaisesMessage(GuardCheckFailed, "non-stock"):
            self._transition(self.case, "awaiting_part")

        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        advance = AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("400")
        )
        rare_order.advance_payment = advance
        rare_order.save()
        case = self._transition(self.case, "awaiting_part")
        self.assertEqual(case.status.code, "awaiting_part")

    # SVC-16: ready_for_handover requires the latest test run to pass.
    def test_ready_for_handover_requires_passing_test_run(self):
        self.case.status = self.statuses["quality_check"]
        self.case.save()

        with self.assertRaisesMessage(GuardCheckFailed, "test run"):
            self._transition(self.case, "ready_for_handover")

        TestRun = django_apps.get_model("service", "TestRun")
        TestRun.objects.create(
            work_order=self.work_order,
            result=TestRun.Result.FAIL,
            performed_at=timezone.now() - timedelta(hours=1),
        )
        with self.assertRaisesMessage(GuardCheckFailed, "did not pass"):
            self._transition(self.case, "ready_for_handover")

        TestRun.objects.create(
            work_order=self.work_order, result=TestRun.Result.PASSED
        )
        case = self._transition(self.case, "ready_for_handover")
        self.assertEqual(case.status.code, "ready_for_handover")


class TransportGuardTests(WorkflowTestBase):
    """TRP-04 / INV-06: paid advance before departure_scheduled."""

    CODES = [
        "inquiry",
        "quote_sent",
        "advance_paid",
        "departure_scheduled",
        "in_transit",
        "delivered",
        "closed",
    ]

    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.TRANSPORT, self.CODES)
        self.case = self._make_case(
            self.statuses, "advance_paid", case_type=CaseType.TRANSPORT
        )

    def _add_paid_advance(self, paid_at):
        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        return AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("300"), paid_at=paid_at
        )

    def _set_departure(self, departure_date):
        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        return TransportOrder.objects.create(
            case=self.case,
            origin="North depot",
            destination="South depot",
            mode=TransportOrder.Mode.OWN_VEHICLE,
            preferred_date=departure_date,
        )

    def test_departure_scheduled_requires_a_departure_date(self):
        """QA T-M2: "Departure scheduled" was reached with no date at all."""
        self._add_paid_advance(paid_at=timezone.now())
        with self.assertRaisesMessage(GuardCheckFailed, "TRP-05"):
            self._transition(self.case, "departure_scheduled")

    def test_departure_scheduled_requires_paid_advance(self):
        with self.assertRaisesMessage(GuardCheckFailed, "advance"):
            self._transition(self.case, "departure_scheduled")

        self._set_departure(timezone.localdate() + timedelta(days=30))
        self._add_paid_advance(paid_at=timezone.now())
        case = self._transition(self.case, "departure_scheduled")
        self.assertEqual(case.status.code, "departure_scheduled")

    def test_late_advance_payment_logs_warning_but_does_not_block(self):
        # Departure in 3 days, paid today: later than 7 days before -> warn.
        self._set_departure(timezone.localdate() + timedelta(days=3))
        self._add_paid_advance(paid_at=timezone.now())

        case = self._transition(self.case, "departure_scheduled")
        self.assertEqual(case.status.code, "departure_scheduled")
        warning = ActivityLog.objects.get(case=self.case)
        self.assertEqual(warning.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("TRP-04", warning.text)

    def test_timely_advance_payment_logs_no_warning(self):
        self._set_departure(timezone.localdate() + timedelta(days=30))
        self._add_paid_advance(paid_at=timezone.now())

        self._transition(self.case, "departure_scheduled")
        self.assertFalse(ActivityLog.objects.filter(case=self.case).exists())


class WinterStorageGuardTests(WorkflowTestBase):
    """WNT-01 / INV-11 and WNT-05."""

    CODES = [
        "inquiry",
        "contracted",
        "estimate_sent",
        "asset_received",
        "in_storage",
        "ready_for_pickup",
        "closed",
    ]

    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.STORAGE, self.CODES)
        self.case = self._make_case(
            self.statuses, "inquiry", case_type=CaseType.STORAGE
        )

    # WNT-01 / WNT-03 / INV-11: contracting happens before the priced
    # estimate, so it needs the documentation, the linked asset and the
    # pricing basis (accommodation + term) all in place.
    def test_contracted_requires_documentation_and_pricing_basis(self):
        with self.assertRaisesMessage(GuardCheckFailed, "asset"):
            self._transition(self.case, "contracted")

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")
        client = Client.objects.create(
            name="Winter Client", phone="+44 67 000 000"
        )
        asset = Asset.objects.create(
            client=client, name="Bora", length_m=Decimal("11.20")
        )
        self.case.client = client
        self.case.asset = asset
        self.case.save()

        # Asset + contact present, but the agreement pricing inputs are
        # still missing (permit, length, accommodation, term).
        with self.assertRaisesMessage(GuardCheckFailed, "accommodation"):
            self._transition(self.case, "contracted")

        from apps.storage.services import upsert_agreement

        upsert_agreement(
            self.case,
            permit_no="SP-1",
            asset_length_m=Decimal("11.20"),
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
            accommodation="OPEN",
            term="SEASONAL",
        )
        self.case.refresh_from_db()
        case = self._transition(self.case, "contracted")
        self.assertEqual(case.status.code, "contracted")

    # WNT-03: estimate_sent needs a sent (non-draft) value estimate.
    def test_estimate_sent_requires_a_sent_quote(self):
        self.case.status = self.statuses["contracted"]
        self.case.save()
        with self.assertRaisesMessage(GuardCheckFailed, "estimate"):
            self._transition(self.case, "estimate_sent")

        from apps.quotes.services import create_quote, send_quote

        quote = create_quote(self.case, "Storage - estimate", Decimal("1350"))
        send_quote(quote, self.user)
        case = self._transition(self.case, "estimate_sent")
        self.assertEqual(case.status.code, "estimate_sent")

    # WNT-05: asset_received needs a signed intake record, no work order.
    def test_asset_received_requires_signed_intake_only(self):
        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")
        client = Client.objects.create(name="Winter Client")
        self.case.client = client
        # §7.2: the six, so ``_guard_asset_received`` has an asset to name.
        self.case.asset = Asset.objects.create(
            client=client,
            name="Bora",
            asset_type="Compressor",
            manufacturer="Bavaria",
            model="Cruiser 37",
            registration_no="TV-2001",
            length_m=Decimal("11.20"),
        )
        self.case.status = self.statuses["estimate_sent"]
        self.case.save()

        with self.assertRaisesMessage(GuardCheckFailed, "intake record"):
            self._transition(self.case, "asset_received")

        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        IntakeRecord.objects.create(
            case=self.case,
            received_by=self.user,
            mount_status="ON_MOUNT",
            signed_by=self.user,
            signed_at=timezone.now(),
        )
        # No WorkOrder exists - must still pass for storage.
        case = self._transition(self.case, "asset_received")
        self.assertEqual(case.status.code, "asset_received")


class RoleFilteredTransitionTests(WorkflowTestBase):
    """STM-06 / ACT-01: allowed_roles restricts who may take a transition."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        # Restrict awaiting_approval -> in_progress to managerial roles.
        self.restricted = StatusTransition.objects.get(
            from_status=self.statuses["awaiting_approval"],
            to_status=self.statuses["in_progress"],
        )
        self.restricted.allowed_roles = ["MANAGER", "DIRECTOR"]
        self.restricted.save()
        self.case = self._make_case(self.statuses, "awaiting_approval")

    def _make_user(self, username, role="", **kwargs):
        User = get_user_model()
        return User.objects.create_user(
            username=username, password="demo12345", is_staff=True,
            role=role, **kwargs,
        )

    def test_available_transitions_includes_allowed_role(self):
        manager = self._make_user("manager", role="MANAGER")
        statuses = available_transitions(self.case, manager)
        self.assertIn(self.statuses["in_progress"], statuses)
        self.assertIn(self.statuses["awaiting_part"], statuses)

    def test_available_transitions_excludes_disallowed_role(self):
        technician = self._make_user("technician", role="TECHNICIAN")
        statuses = available_transitions(self.case, technician)
        self.assertNotIn(self.statuses["in_progress"], statuses)
        # Unrestricted (empty allowed_roles) edges remain visible.
        self.assertIn(self.statuses["awaiting_part"], statuses)

    def test_available_transitions_empty_list_is_unrestricted(self):
        technician = self._make_user("technician", role="TECHNICIAN")
        self.restricted.allowed_roles = []
        self.restricted.save()
        statuses = available_transitions(self.case, technician)
        self.assertIn(self.statuses["in_progress"], statuses)

    def test_available_transitions_superuser_bypasses_role_check(self):
        User = get_user_model()
        root = User.objects.create_superuser(
            username="root", password="demo12345"
        )
        statuses = available_transitions(self.case, root)
        self.assertIn(self.statuses["in_progress"], statuses)

    def test_available_transitions_none_user_is_unrestricted(self):
        statuses = available_transitions(self.case, None)
        self.assertIn(self.statuses["in_progress"], statuses)

    def test_transition_case_raises_role_not_allowed(self):
        technician = self._make_user("technician", role="TECHNICIAN")
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaisesMessage(RoleNotAllowed, "TECHNICIAN"):
                    transition_case(self.case, "in_progress", technician)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["awaiting_approval"])
        self.assertEqual(StatusHistory.objects.count(), 0)

    def test_transition_case_allows_permitted_role_through_role_check(self):
        # The role check passes for a manager; the transition then proceeds
        # to guard checks (quote approval, SVC-13), proving the refusal
        # above was the role check and not something else.
        manager = self._make_user("boss", role="MANAGER")
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(GuardCheckFailed):
                    transition_case(self.case, "in_progress", manager)


class AppendOnlyTests(WorkflowTestBase):
    """INV-03: StatusHistory and ActivityLog are append-only."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "new_report")

    def test_status_history_cannot_be_updated_or_deleted(self):
        entry = StatusHistory.objects.create(
            case=self.case,
            from_status=None,
            to_status=self.statuses["new_report"],
            changed_by=self.user,
        )
        entry.note = "tampered"
        with self.assertRaises(AppendOnlyViolation):
            entry.save()
        with self.assertRaises(AppendOnlyViolation):
            entry.delete()

    def test_activity_log_cannot_be_updated_or_deleted(self):
        entry = ActivityLog.objects.create(
            case=self.case, author=self.user, text="Called the client."
        )
        entry.text = "tampered"
        with self.assertRaises(AppendOnlyViolation):
            entry.save()
        with self.assertRaises(AppendOnlyViolation):
            entry.delete()

    def test_case_delete_still_cascades_over_audit_rows(self):
        ActivityLog.objects.create(case=self.case, text="note")
        self.case.delete()
        self.assertFalse(ActivityLog.objects.exists())


class TransitionNotificationTests(WorkflowTestBase):
    """NTF-03: notification fires only after the transaction commits."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "new_report")

    def test_notification_scheduled_on_commit(self):
        self._add_intake_appointment(self.case)
        with patch("apps.notifications.services.notify_case_transition") as mocked:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                transition_case(self.case, "intake_scheduled", self.user)
                # Inside the transaction nothing has been sent yet.
                mocked.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
            mocked.assert_called_once_with(
                self.case, self.statuses["new_report"], self.statuses["intake_scheduled"]
            )

    def test_failed_transition_schedules_no_notification(self):
        with patch("apps.notifications.services.notify_case_transition") as mocked:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(GuardCheckFailed):
                    transition_case(self.case, "intake_scheduled", self.user)
            self.assertEqual(callbacks, [])
            mocked.assert_not_called()


class SetFinancialSettlementTests(TestCase):
    """SVC-19 / INV-07: settlement flag flips are role-gated and logged."""

    def setUp(self):
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="ready_for_handover",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        User = get_user_model()
        self.manager = User.objects.create_user(
            username="manager", password="demo12345", role="MANAGER"
        )
        self.technician = User.objects.create_user(
            username="technician", password="demo12345", role="TECHNICIAN"
        )
        self.superuser = User.objects.create_superuser(
            username="admin", password="demo12345"
        )

    def test_manager_settles_and_system_log_written(self):
        set_financial_settlement(self.case, self.manager, True, note="Paid cash.")
        self.case.refresh_from_db()
        self.assertTrue(self.case.financially_settled)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertEqual(entry.author, self.manager)
        self.assertIn("settled", entry.text)
        self.assertIn("Paid cash.", entry.text)

    def test_technician_is_refused(self):
        with self.assertRaises(DjangoValidationError):
            set_financial_settlement(self.case, self.technician, True)
        self.case.refresh_from_db()
        self.assertFalse(self.case.financially_settled)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_superuser_bypasses_role_check(self):
        set_financial_settlement(self.case, self.superuser, True)
        self.case.refresh_from_db()
        self.assertTrue(self.case.financially_settled)

    def test_noop_flip_is_refused(self):
        with self.assertRaises(DjangoValidationError):
            set_financial_settlement(self.case, self.manager, False)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_unsettling_works_and_is_logged(self):
        set_financial_settlement(self.case, self.manager, True)
        set_financial_settlement(self.case, self.manager, False, note="Refund issued.")
        self.case.refresh_from_db()
        self.assertFalse(self.case.financially_settled)
        self.assertEqual(ActivityLog.objects.count(), 2)
        self.assertIn("NOT settled", ActivityLog.objects.order_by("pk").last().text)


class SeedWorkflowConfigTests(TestCase):
    """The seed command creates config data once and never clobbers
    staff-entered values on re-runs."""

    def _seed(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("seed_workflow", *args, stdout=out)
        return out.getvalue()

    def test_configuration_seeds_once(self):
        self._seed()
        StandardDepartureDay = django_apps.get_model(
            "transport", "StandardDepartureDay"
        )
        InspectionPoint = django_apps.get_model("service", "InspectionPoint")
        ApprovalLimit = django_apps.get_model("accounts", "ApprovalLimit")
        counts = (
            Status.objects.count(),
            StandardDepartureDay.objects.count(),
            InspectionPoint.objects.count(),
            ApprovalLimit.objects.count(),
        )
        self.assertEqual(counts[1:], (3, 4, 3))
        self.assertGreater(counts[0], 0)
        output = self._seed()
        self.assertEqual(
            (
                Status.objects.count(),
                StandardDepartureDay.objects.count(),
                InspectionPoint.objects.count(),
                ApprovalLimit.objects.count(),
            ),
            counts,
        )
        self.assertIn("0 new statuses, 0 new transitions", output)

    def test_seeds_no_price_lists_users_or_sample_data(self):
        """The template ships workflow configuration only."""
        self._seed()
        User = get_user_model()
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(django_apps.get_model("clients", "Client").objects.count(), 0)
        self.assertEqual(Case.objects.count(), 0)
        for app_label, model in [
            ("transport", "TransportPriceMatrix"),
            ("storage", "StoragePriceBand"),
            ("service", "ServicePriceItem"),
        ]:
            self.assertEqual(
                django_apps.get_model(app_label, model).objects.count(), 0, model
            )

    def test_staff_edits_survive_reseed(self):
        self._seed()
        StandardDepartureDay = django_apps.get_model(
            "transport", "StandardDepartureDay"
        )
        ApprovalLimit = django_apps.get_model("accounts", "ApprovalLimit")

        day = StandardDepartureDay.objects.get(code="SAT")
        day.is_active = False
        day.save()
        limit = ApprovalLimit.objects.get(role="MANAGER")
        limit.max_amount = Decimal("2500")
        limit.save()

        self._seed()
        day.refresh_from_db()
        limit.refresh_from_db()
        self.assertFalse(day.is_active)
        self.assertEqual(limit.max_amount, Decimal("2500"))


class WinterStorageInquiryGuardTests(WorkflowTestBase):
    """WNT-01/WNT-03: the inquiry documentation (contact + linked asset +
    permit + asset length + accommodation + term) gates
    ``inquiry -> contracted`` for STORAGE - the storage is
    contracted before the priced estimate goes out."""

    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "contracted"]
        )

    def _storage_case(self, **kwargs):
        return self._make_case(
            self.statuses,
            "inquiry",
            case_type=CaseType.STORAGE,
            **kwargs,
        )

    def test_contracted_requires_documentation(self):
        from apps.storage.services import upsert_agreement

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        client = Client.objects.create(name="Mark", phone="+44 67 1")
        asset = Asset.objects.create(
            client=client, name="Bora", length_m=Decimal("8.50")
        )
        case = self._storage_case(raw_phone="+44 67 123 456")
        case.asset = asset
        case.save()

        with self.assertRaises(GuardCheckFailed) as ctx:
            self._transition(case, "contracted")
        self.assertIn("permit", str(ctx.exception))
        self.assertIn("length", str(ctx.exception))

        upsert_agreement(case, permit_no="MNE-1234")
        case.refresh_from_db()
        with self.assertRaises(GuardCheckFailed) as ctx:
            self._transition(case, "contracted")
        self.assertNotIn("permit", str(ctx.exception))
        self.assertIn("length", str(ctx.exception))

        upsert_agreement(
            case,
            asset_length_m=Decimal("8.50"),
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
            accommodation="OPEN",
            term="SEASONAL",
        )
        case.refresh_from_db()
        self._transition(case, "contracted")
        self.assertEqual(case.status.code, "contracted")

    def test_contracted_requires_the_database_entry(self):
        """WNT-02 (procedure §5.2): the site, the ownership and the
        selected services are named by the procedure as things recorded in
        the database. They used to be demanded only by the WNT-02 page,
        which nothing routes a case through."""
        from apps.storage.services import upsert_agreement

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        client = Client.objects.create(name="Mark", phone="+44 67 1")
        asset = Asset.objects.create(
            client=client, name="Bora", length_m=Decimal("8.50")
        )
        case = self._storage_case(raw_phone="+44 67 123 456")
        case.asset = asset
        case.save()
        upsert_agreement(
            case,
            permit_no="MNE-1234",
            asset_length_m=Decimal("8.50"),
            accommodation="OPEN",
            term="SEASONAL",
        )
        case.refresh_from_db()
        with self.assertRaises(GuardCheckFailed) as ctx:
            self._transition(case, "contracted")
        message = str(ctx.exception)
        self.assertIn("site", message)
        self.assertIn("ownership", message)
        self.assertIn("selected service", message)

        upsert_agreement(
            case,
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
        )
        case.refresh_from_db()
        self._transition(case, "contracted")
        self.assertEqual(case.status.code, "contracted")

    def test_contracted_requires_contact(self):
        from apps.storage.services import upsert_agreement

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        asset = Asset.objects.create(
            client=Client.objects.create(name="Owner"),
            name="Bora",
            length_m=Decimal("8.50"),
        )
        # Explicitly unreachable: ``_make_case`` gives its reporter a
        # number by default, and this test is about the absence.
        case = self._storage_case(raw_phone="", raw_email="")
        case.asset = asset
        case.save()
        upsert_agreement(
            case,
            permit_no="MNE-1234",
            asset_length_m=Decimal("8.50"),
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
            accommodation="OPEN",
            term="SEASONAL",
        )
        case.refresh_from_db()
        with self.assertRaises(GuardCheckFailed) as ctx:
            self._transition(case, "contracted")
        self.assertIn("contact", str(ctx.exception))

        case.client = Client.objects.create(name="Mark", phone="+44 67 1")
        case.save()
        self._transition(case, "contracted")
        self.assertEqual(case.status.code, "contracted")

    def test_transport_quote_sent_not_affected(self):
        from apps.quotes.models import Quote
        from apps.transport.models import TransportOrder

        transport_statuses = build_workflow(
            CaseType.TRANSPORT, ["inquiry", "quote_sent"]
        )
        case = self._make_case(
            transport_statuses, "inquiry", case_type=CaseType.TRANSPORT
        )
        # A transport case has its own reasons to be stopped here (TRP-01
        # wants a documented inquiry and a sent offer), so give it those.
        # What this test pins is that it is NOT stopped for a storage
        # reason: no agreement, no permit, no contact requirements.
        TransportOrder.objects.create(
            case=case,
            origin="Customer site",
            destination="Zone A",
            asset_length_m=Decimal("8.50"),
        )
        Quote.objects.create(
            case=case,
            description="Transport Northtown - Zone A",
            amount=Decimal("1200.00"),
            status=Quote.Status.SENT,
        )
        self._transition(case, "quote_sent")
        self.assertEqual(case.status.code, "quote_sent")


class CaseAttachmentsAggregationTests(WorkflowTestBase):
    """``case_attachments`` gathers a work object's whole media set, not just
    the files hanging directly off the ``Case`` row (Phase 1 media view)."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "new_report")
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)

    def _attach(self, obj, name, kind, stage):
        from apps.attachments.services import attach_file

        return attach_file(
            obj, SimpleUploadedFile(name, b"x"), kind, stage, self.user
        )

    def test_aggregates_files_from_case_and_child_records(self):
        from apps.attachments.models import Attachment

        from .services import case_attachments

        WorkOrder = django_apps.get_model("service", "WorkOrder")
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        HandoverRecord = django_apps.get_model("intake", "HandoverRecord")

        work_order = WorkOrder.objects.create(
            case=self.case,
            is_preliminary=False,
            signed_by_technician=self.user,
            signed_at=timezone.now(),
        )
        intake = IntakeRecord.objects.create(
            case=self.case,
            received_by=self.user,
            mount_status="OFF_MOUNT",
            signed_by=self.user,
            signed_at=timezone.now(),
        )
        handover = HandoverRecord.objects.create(
            case=self.case,
            handed_over_by=self.user,
            received_by_name="Test Contact",
            signed_at=timezone.now(),
        )

        with override_settings(MEDIA_ROOT=self.media_root):
            on_case = self._attach(
                self.case, "case.jpg", Attachment.Kind.PHOTO, Attachment.Stage.REPORT
            )
            on_order = self._attach(
                work_order, "approval.pdf",
                Attachment.Kind.DOCUMENT, Attachment.Stage.WORK,
            )
            on_intake = self._attach(
                intake, "intake.mp4", Attachment.Kind.VIDEO, Attachment.Stage.INTAKE
            )
            on_handover = self._attach(
                handover, "handover.pdf",
                Attachment.Kind.DOCUMENT, Attachment.Stage.HANDOVER,
            )

            collected = list(case_attachments(self.case))

        # Every child record's file is present, not just the case's own.
        self.assertCountEqual(
            collected, [on_case, on_order, on_intake, on_handover]
        )

    def test_only_this_case(self):
        """Files on a sibling case must not leak into the aggregate."""
        from apps.attachments.models import Attachment

        from .services import case_attachments

        other = self._make_case(self.statuses, "new_report")
        with override_settings(MEDIA_ROOT=self.media_root):
            mine = self._attach(
                self.case, "mine.jpg",
                Attachment.Kind.PHOTO, Attachment.Stage.REPORT,
            )
            self._attach(
                other, "theirs.jpg",
                Attachment.Kind.PHOTO, Attachment.Stage.REPORT,
            )
            collected = list(case_attachments(self.case))

        self.assertEqual(collected, [mine])


class AutoAdvanceCaseTests(TestCase):
    """``auto_advance_case`` walks *linear* edges only, so staff never
    click a transition that involves no decision. It stops at a branch, a
    role-gated edge, an unmet guard and a terminal target.

    A hermetic workflow with codes outside the real ``GUARDS`` registry is
    built inline so no seeded guard interferes; a guard is injected via
    ``patch`` only where a stop-on-guard is under test.
    """

    def setUp(self):
        User = get_user_model()
        self.tech = User.objects.create_user(
            username="aa_tech", password="demo12345", is_staff=True, role="TECHNICIAN"
        )
        # Hermetic codes (aa_*) never collide with the real GUARDS keys.
        codes = ["aa_a", "aa_b", "aa_c", "aa_d1", "aa_d2", "aa_z"]
        # Linear chain aa_a->..->aa_z, plus a second edge out of aa_c so
        # aa_c is a branch (a human must choose there).
        self.st = build_workflow(CaseType.SERVICE, codes, extra_edges=[("aa_c", "aa_d2")])

    def _case(self, code):
        return Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.st[code],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Auto Advance",
            location="Customer site",
        )

    def _advance(self, case):
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                return auto_advance_case(case, self.tech)

    def test_walks_linear_chain_and_stops_at_branch(self):
        case = self._case("aa_a")
        advanced = self._advance(case)
        self.assertEqual([s.code for s in advanced], ["aa_b", "aa_c"])
        case.refresh_from_db()
        self.assertEqual(case.status.code, "aa_c")  # branch: choice left to a human

    def test_stops_before_terminal_never_auto_closes(self):
        case = self._case("aa_d1")  # aa_d1 -> aa_d2 -> aa_z(terminal)
        advanced = self._advance(case)
        self.assertEqual([s.code for s in advanced], ["aa_d2"])
        case.refresh_from_db()
        self.assertEqual(case.status.code, "aa_d2")  # never steps into terminal

    def test_stops_at_role_gated_edge(self):
        # A manager-only edge is an authorization decision, not a linear step.
        edge = StatusTransition.objects.get(
            from_status=self.st["aa_b"], to_status=self.st["aa_c"]
        )
        edge.allowed_roles = ["MANAGER", "DIRECTOR"]
        edge.save()
        case = self._case("aa_a")
        advanced = self._advance(case)
        self.assertEqual([s.code for s in advanced], ["aa_b"])
        case.refresh_from_db()
        self.assertEqual(case.status.code, "aa_b")

    def test_stops_at_unmet_guard(self):
        def _blocked(case, from_status):
            raise GuardCheckFailed("not ready")

        with patch.dict("apps.cases.services.GUARDS", {"aa_c": [_blocked]}, clear=False):
            case = self._case("aa_a")
            advanced = self._advance(case)
        self.assertEqual([s.code for s in advanced], ["aa_b"])
        case.refresh_from_db()
        self.assertEqual(case.status.code, "aa_b")

    def test_no_op_when_current_status_branches(self):
        case = self._case("aa_c")  # already at the branch
        advanced = self._advance(case)
        self.assertEqual(advanced, [])
        case.refresh_from_db()
        self.assertEqual(case.status.code, "aa_c")
