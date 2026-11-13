"""Tests for the guided-workflow engine (apps.backoffice.guide).

Each walkthrough also performs the real transition once the guide says
it is enabled - pinning the guide predicates to the actual guards.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.backoffice.guide import GuideContext, build_guide
from apps.cases.models import (
    Case,
    CaseType,
    ContactRole,
    ReportChannel,
    Status,
    StatusTransition,
    WorkLocation,
)
from apps.cases.services import record_work_location_consult, transition_case
from apps.scheduling.workinghours import next_working_slot
from apps.cases.tests import build_service_workflow, build_workflow
from apps.clients.tests import link_identified_asset

TRANSPORT_CODES = [
    "inquiry",
    "quote_sent",
    "advance_paid",
    "departure_scheduled",
    "in_transit",
    "delivered",
    "closed",
]
# WNT-03: the storage is contracted before the priced estimate goes out,
# so the chain has no pre-contract quote_sent - see seed_workflow.
STORAGE_CODES = [
    "inquiry",
    "contracted",
    "estimate_sent",
    "asset_received",
    "in_storage",
    "ready_for_pickup",
    "closed",
]


def _reachable_client(name="Contact Client"):
    """§7.1: a client with contact data, so an OWNER designation on a
    fixture actually resolves to somebody."""
    from apps.clients.models import Client

    return Client.objects.create(
        name=name, phone="+44 68 999 888", email="kontakt@example.com"
    )


def ctx_for(case):
    """Build a GuideContext the way the detail view does."""
    from apps.attachments.services import attachments_for

    return GuideContext(
        case,
        appointments=list(case.appointments.all()),
        work_order=getattr(case, "work_order", None),
        intake_records=list(case.intake_records.all()),
        attachments=list(attachments_for(case)),
        quotes=list(case.quotes.order_by("-created_at").prefetch_related("approvals")),
        advance_payments=list(case.advance_payments.all()),
        part_orders=list(case.part_orders.select_related("part")),
        handover=case.handover_records.order_by("-created_at", "-pk").first(),
        transport_order=getattr(case, "transport_order", None),
    )


class GuideTestBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.technician = User.objects.create_user(
            username="tech", password="x", is_staff=True, role="TECHNICIAN"
        )
        self.manager = User.objects.create_user(
            username="mgr", password="x", is_staff=True, role="MANAGER"
        )

    def guide(self, case, user=None):
        return build_guide(case, user or self.manager, ctx_for(case))

    def step(self, guide, key):
        return next(s for s in guide["steps"] if s.key == key)

    def transition(self, guide, code):
        return next(t for t in guide["transitions"] if t.code == code)

    def _do_transition(self, case, code, user=None):
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                transition_case(case, code, user or self.manager)


class ServiceGuideTests(GuideTestBase):
    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        restricted = StatusTransition.objects.get(
            from_status=self.statuses["awaiting_approval"],
            to_status=self.statuses["in_progress"],
        )
        restricted.allowed_roles = ["MANAGER", "DIRECTOR"]
        restricted.save()
        closing = StatusTransition.objects.get(
            from_status=self.statuses["ready_for_handover"],
            to_status=self.statuses["closed"],
        )
        closing.allowed_roles = ["MANAGER", "DIRECTOR"]
        closing.save()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["new_report"],
            location="Customer site",  # §7.3
            # §7.1: designated AND reachable - the guard asks that the
            # designation still resolves to somebody. Answered through a
            # linked client rather than raw_* fields, which would make
            # the case look like an untriaged portal submission and
            # change which step the hero picks.
            client=_reachable_client(),
            contact_role=ContactRole.OWNER,
        )

    def test_new_report_needs_intake_appointment(self):
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "book_intake_appointment").done)
        self.assertFalse(self.transition(guide, "intake_scheduled").enabled)

        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.INTAKE,
            scheduled_for=timezone.now() + timedelta(days=1),
        )
        # SVC-04: the appointment alone no longer opens the transition -
        # the way the asset gets here is the second half of the same
        # conversation and blocks alongside it.
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "book_intake_appointment").done)
        self.assertFalse(self.step(guide, "record_delivery_method").done)
        self.assertFalse(self.transition(guide, "intake_scheduled").enabled)

        self.case.delivery_method = "CLIENT_SHIPS"
        self.case.save(update_fields=["delivery_method"])
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "record_delivery_method").done)
        self.assertTrue(self.transition(guide, "intake_scheduled").enabled)
        self._do_transition(self.case, "intake_scheduled")  # guard parity

    def test_intake_scheduled_walkthrough(self):
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        from apps.intake.services import (
            confirm_no_intake_items,
            create_intake_record,
            sign_intake_record,
        )
        from apps.service.services import (
            attach_client_approval,
            create_work_order,
            finalize_work_order,
            sign_work_order,
        )

        self.case.status = self.statuses["intake_scheduled"]
        self.case.save()
        # §7.2: a receipt has to say which asset it is for. Linked here
        # rather than in setUp, because this class also tests the guide
        # on a case triage has not touched yet.
        link_identified_asset(self.case)

        guide = self.guide(self.case)
        # 7 before §7.2 added "identify_asset"; the fixture's asset is
        # fully identified, so the eighth is already ticked off.
        self.assertEqual(len([s for s in guide["steps"] if not s.done]), 7)
        self.assertTrue(self.step(guide, "identify_asset").done)
        self.assertFalse(self.transition(guide, "asset_received").enabled)

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        work_order = create_work_order(self.case, self.manager)
        finalize_work_order(work_order, self.manager)
        with override_settings(MEDIA_ROOT=media_root):
            attach_client_approval(
                work_order,
                SimpleUploadedFile("work-order.pdf", b"%PDF-1.4 scan"),
                self.manager,
            )
        sign_work_order(work_order, self.technician)
        record = create_intake_record(
            self.case, self.manager, mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN", storage_location="Hall 1",
            damage_note="none", energy_level="HALF",  # §7.6
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "upload_client_approval").done)
        self.assertTrue(self.step(guide, "sign_work_order").done)
        self.assertFalse(self.step(guide, "upload_arrival_photos").done)
        self.assertFalse(self.step(guide, "sign_intake_record").done)
        self.assertFalse(self.transition(guide, "asset_received").enabled)

        # SVC-07: the arrival photo both completes the guide step and
        # unlocks the receipt signature.
        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file

        with override_settings(MEDIA_ROOT=media_root):
            attach_file(
                self.case,
                SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
                Attachment.Kind.PHOTO,
                Attachment.Stage.INTAKE,
                user=self.manager,
            )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "upload_arrival_photos").done)

        confirm_no_intake_items(record, self.manager)  # SVC-06
        sign_intake_record(record, self.manager)
        guide = self.guide(self.case)
        self.assertTrue(all(s.done for s in guide["steps"]))
        self.assertTrue(self.transition(guide, "asset_received").enabled)
        self.assertEqual(guide["hero_kind"], "transition")
        self._do_transition(self.case, "asset_received")  # guard parity

    def test_diagnostics_gates_both_edges(self):
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        self.case.status = self.statuses["diagnostics"]
        self.case.save()
        # Returning client: the SVC-08 inspection does not apply, so this
        # test isolates the diagnostics gate. New-client inspection gating is
        # covered by test_new_client_inspection_gates_awaiting_approval.
        Client = django_apps.get_model("clients", "Client")
        client = Client.objects.create(name="Stalni")
        self.case.client = client
        self.case.save()
        Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["closed"],
            client=client,
        )
        work_order = WorkOrder.objects.create(case=self.case)

        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "record_diagnostics").done)
        self.assertFalse(self.transition(guide, "awaiting_approval").enabled)
        self.assertFalse(self.transition(guide, "in_progress").enabled)
        # The step points at the standalone diagnostics card, not admin.
        self.assertEqual(
            self.step(guide, "record_diagnostics").anchor, "acc-diagnostics"
        )

        Diagnostic.objects.create(
            work_order=work_order,
            issue="Pumpa",
            servicing_work="Zamjena pumpe",
            parts_needed="Pumpa za vodu",
            estimated_parts_price=Decimal("120.00"),
            estimated_completion_date=timezone.now().date(),
        )
        guide = self.guide(self.case)
        self.assertTrue(self.transition(guide, "awaiting_approval").enabled)
        in_progress = self.transition(guide, "in_progress")
        self.assertTrue(in_progress.enabled)
        self.assertFalse(in_progress.primary)

    def test_awaiting_approval_pipeline_and_role_gate(self):
        from apps.quotes.services import create_quote, record_approval, send_quote

        django_apps.get_model("accounts", "ApprovalLimit").objects.create(
            role="TECHNICIAN", max_amount=Decimal("100")
        )
        django_apps.get_model("accounts", "ApprovalLimit").objects.create(
            role="MANAGER", max_amount=Decimal("1000")
        )
        self.case.status = self.statuses["awaiting_approval"]
        self.case.save()

        guide = self.guide(self.case, self.technician)
        self.assertFalse(self.step(guide, "create_quote").done)

        quote = create_quote(self.case, "Popravka pumpe", Decimal("500"))
        send_quote(quote, self.manager)
        record_approval(quote, "client", "approved", user=self.manager)
        guide = self.guide(self.case, self.technician)
        self.assertTrue(self.step(guide, "client_approval").done)
        self.assertFalse(self.step(guide, "internal_approvals").done)

        record_approval(quote, "manager", "approved", user=self.manager)
        guide = self.guide(self.case, self.technician)
        self.assertTrue(self.step(guide, "internal_approvals").done)
        # Technician cannot take the managerial edge: waiting state.
        edge = self.transition(guide, "in_progress")
        self.assertFalse(edge.role_allowed)
        self.assertFalse(edge.enabled)
        self.assertIn("Manager", str(edge.waiting_role))
        self.assertEqual(guide["hero_kind"], "waiting")

        guide = self.guide(self.case, self.manager)
        self.assertTrue(self.transition(guide, "in_progress").enabled)
        self._do_transition(self.case, "in_progress")  # guard parity

    def test_awaiting_part_edge_gated_by_part_steps(self):
        Part = django_apps.get_model("parts", "Part")
        PartOrder = django_apps.get_model("parts", "PartOrder")
        self.case.status = self.statuses["awaiting_approval"]
        self.case.save()

        guide = self.guide(self.case)
        self.assertFalse(self.transition(guide, "awaiting_part").enabled)

        part = Part.objects.create(code="IMP-1", name="Impeler", is_consumable=False)
        PartOrder.objects.create(
            case=self.case, part=part, expected_delivery=date.today()
        )
        guide = self.guide(self.case)
        # Non-stock order without a linked advance keeps the edge gated.
        self.assertTrue(self.step(guide, "order_part").done)
        self.assertFalse(self.step(guide, "part_advance").done)
        self.assertFalse(self.transition(guide, "awaiting_part").enabled)

        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        advance = AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("250")
        )
        order = PartOrder.objects.get()
        order.advance_payment = advance
        order.save()
        guide = self.guide(self.case)
        self.assertTrue(self.transition(guide, "awaiting_part").enabled)

    def test_over_budget_work_can_still_go_back_for_a_new_quote(self):
        """SVC-14: when the logged work no longer fits the approved amount,
        Kontrola is (rightly) gated - but the way out, back to
        awaiting_approval for a new quote, must stay open or the case is
        locked in place."""
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        self.case.status = self.statuses["in_progress"]
        self.case.save()
        WorkOrder.objects.create(case=self.case, is_preliminary=False)
        # No accepted quote and no technician limit: nothing authorises
        # the work, so the within-amount step reads as not done.
        django_apps.get_model("accounts", "ApprovalLimit").objects.all().delete()

        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "within_approved_amount").done)
        self.assertFalse(self.transition(guide, "quality_check").enabled)
        self.assertTrue(self.transition(guide, "awaiting_approval").enabled)
        self._do_transition(self.case, "awaiting_approval")  # guard parity
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "awaiting_approval")

    def test_quality_check_latest_run_semantics(self):
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        TestRun = django_apps.get_model("service", "TestRun")
        self.case.status = self.statuses["quality_check"]
        self.case.save()
        work_order = WorkOrder.objects.create(case=self.case)

        TestRun.objects.create(work_order=work_order, result="FAIL")
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "record_pass_test_run").done)

        TestRun.objects.create(work_order=work_order, result="PASS")
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "record_pass_test_run").done)
        self.assertTrue(self.transition(guide, "ready_for_handover").enabled)

    def test_ready_for_handover_closing_checklist(self):
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file
        from apps.cases.services import set_financial_settlement
        from apps.intake.services import (
            attach_handover_approval,
            create_handover_record,
            sign_handover,
            sign_handover_technician,
        )

        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()
        # SVC-18: the handover record is drawn up after the work is finished, so
        # a case parked at this status by hand still needs the signed
        # work order the transitions would have insisted on.
        django_apps.get_model("service", "WorkOrder").objects.create(
            case=self.case, is_preliminary=False, signed_at=timezone.now()
        )

        guide = self.guide(self.case, self.technician)
        settle = self.step(guide, "mark_settled")
        self.assertFalse(settle.done)
        self.assertFalse(settle.actionable)
        self.assertIn("Manager", str(settle.waiting_role))

        # SVC-18: receipt confirmation needs the worker's in-app
        # signature and the client-signed scan first.
        handover = create_handover_record(self.case, self.manager)
        sign_handover_technician(handover, self.technician)
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            # §7/SVC-07: photographed on the way out, not only on the
            # way in - and filed on the record, because a case can hold
            # several handovers and each is photographed for itself.
            attach_file(
                handover,
                SimpleUploadedFile("handover.jpg", b"fake-jpeg"),
                Attachment.Kind.PHOTO,
                Attachment.Stage.HANDOVER,
                user=self.manager,
            )
            attach_handover_approval(
                handover,
                SimpleUploadedFile("handover.pdf", b"%PDF-1.4 scan"),
                self.manager,
            )
        sign_handover(handover, "Client")
        set_financial_settlement(self.case, self.manager, True)
        # SVC-17 (advisory step): the client was notified of completion.
        CompletionNotice = django_apps.get_model("service", "CompletionNotice")
        CompletionNotice.objects.create(
            case=self.case,
            works_performed="Engine serviced.",
            final_cost=Decimal("100.00"),
            handover_mode="PICKUP",
            scheduled_for=timezone.now() + timedelta(days=1),
            channel="SMS",
        )
        guide = self.guide(self.case, self.manager)
        self.assertTrue(all(s.done for s in guide["steps"]))
        self.assertTrue(self.transition(guide, "closed").enabled)
        self._do_transition(self.case, "closed")  # guard parity

        guide = self.guide(self.case)
        self.assertEqual(guide["hero_kind"], "done")

    def test_stepper_states(self):
        self.case.status = self.statuses["diagnostics"]
        self.case.save()
        stepper = self.guide(self.case)["stepper"]
        states = {i["status"].code: i["state"] for i in stepper}
        self.assertEqual(states["new_report"], "done")
        self.assertEqual(states["diagnostics"], "current")
        self.assertEqual(states["closed"], "upcoming")

    def test_triage_step_injected_for_unlinked_portal_case(self):
        # The real shape of an untriaged portal case: raw details, no
        # client yet, and §7.1 answered through whoever reported it.
        self.case.client = None
        self.case.raw_contact_name = "Mark"
        self.case.raw_phone = "+44 67 111 222"
        self.case.contact_role = ContactRole.REPORTER
        self.case.save()
        guide = self.guide(self.case)
        self.assertEqual(guide["steps"][0].key, "triage")
        self.assertEqual(guide["hero_kind"], "triage")
        self.assertIn("/triage/", guide["steps"][0].url)

    def test_unknown_status_falls_back_to_db_transitions(self):
        on_hold = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="on_hold",
            name="On hold",
            client_visible_name="On hold",
            sort_order=99,
        )
        StatusTransition.objects.create(
            from_status=on_hold, to_status=self.statuses["in_progress"]
        )
        self.case.status = on_hold
        self.case.save()
        guide = self.guide(self.case)
        self.assertFalse(guide["known"])
        self.assertEqual(guide["hero_kind"], "fallback")
        self.assertEqual(guide["transitions"][0].code, "in_progress")
        self.assertTrue(guide["transitions"][0].enabled)

    def test_new_client_inspection_gates_awaiting_approval(self):
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        InspectionPoint = django_apps.get_model("service", "InspectionPoint")
        from apps.service.services import (
            record_inspection_result, sign_inspection_record, start_inspection,
        )

        self.case.status = self.statuses["diagnostics"]
        self.case.save()
        work_order = WorkOrder.objects.create(case=self.case)
        Diagnostic.objects.create(
            work_order=work_order, issue="Pumpa", servicing_work="Zamjena",
            parts_needed="Pumpa", estimated_parts_price=Decimal("120"),
            estimated_completion_date=timezone.now().date(),
        )
        InspectionPoint.objects.create(system="STRUCTURE", label="Structure", sort_order=0)

        # Diagnostic done, but a first-time client still needs the inspection.
        guide = self.guide(self.case)
        insp = self.step(guide, "new_client_inspection")
        self.assertFalse(insp.done)
        self.assertEqual(insp.anchor, "acc-inspection")
        self.assertFalse(self.transition(guide, "awaiting_approval").enabled)
        # SVC-10 shortcut stays open - it is gated only by the diagnostic.
        self.assertTrue(self.transition(guide, "in_progress").enabled)

        record = start_inspection(work_order, self.manager)
        for item in record.items.all():
            record_inspection_result(item, self.manager, status="OK")
        sign_inspection_record(record, self.manager)

        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "new_client_inspection").done)
        self.assertTrue(self.transition(guide, "awaiting_approval").enabled)
        self._do_transition(self.case, "awaiting_approval")  # guard parity


class TransportGuideTests(GuideTestBase):
    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
        )

    def test_offer_steps_block_quote_sent(self):
        # TRP-01/TRP-04: unlike SERVICE, the offer steps block here - the
        # mandatory 50% advance is derived from the sent offer, so leaving
        # `inquiry` without one dead-ends against the INV-06 guard later.
        from apps.quotes.services import send_quote
        from apps.transport.services import create_transport_offer

        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        # TRP-01 names the asset among the inquiry data, so the step
        # only ticks once the order actually carries one.
        TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode="OWN_VEHICLE", asset_length_m=Decimal("8.50"),
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "record_transport_inquiry").done)
        self.assertFalse(self.step(guide, "create_quote").done)
        self.assertFalse(self.transition(guide, "quote_sent").enabled)

        quote = create_transport_offer(
            self.case, description="Transport",amount=Decimal("2000")
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "create_quote").done)
        self.assertFalse(self.step(guide, "send_quote").done)
        self.assertFalse(self.transition(guide, "quote_sent").enabled)

        send_quote(quote, self.manager)
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "send_quote").done)
        self.assertTrue(self.transition(guide, "quote_sent").enabled)

    def test_quote_sent_blocks_until_the_advance_is_derived(self):
        # TRP-04: `advance_paid` has nothing to confirm without an advance.
        from apps.quotes.services import send_quote
        from apps.transport.services import (
            create_transport_offer,
            ensure_transport_advance,
        )

        self.case.status = self.statuses["quote_sent"]
        self.case.save()
        quote = create_transport_offer(
            self.case, description="Transport",amount=Decimal("2000")
        )
        send_quote(quote, self.manager)
        self.case.advance_payments.all().delete()  # offer sent before the rule

        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "request_advance").done)
        self.assertFalse(self.transition(guide, "advance_paid").enabled)

        advance = ensure_transport_advance(self.case, user=self.manager)
        self.assertEqual(advance.amount, Decimal("1000.00"))
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "request_advance").done)
        # QA T-M1: the status says PAID, and so does the guard - an unpaid
        # row does not enable the move.
        self.assertFalse(self.step(guide, "mark_advance_paid").done)
        self.assertFalse(self.transition(guide, "advance_paid").enabled)

        from apps.quotes.services import mark_advance_paid

        mark_advance_paid(advance, self.manager)
        guide = self.guide(self.case)
        self.assertTrue(self.transition(guide, "advance_paid").enabled)
        self._do_transition(self.case, "advance_paid")  # guard parity

    def test_unrecorded_inquiry_blocks_quote_sent(self):
        # The blocking half of the pair above: no TransportOrder, no move.
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "record_transport_inquiry").done)
        self.assertFalse(self.transition(guide, "quote_sent").enabled)

    def test_advance_paid_blocks_departure_until_paid(self):
        from apps.quotes.services import mark_advance_paid

        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        order = TransportOrder.objects.create(
            case=self.case,
            origin="North depot",
            destination="South depot",
            mode="EXTERNAL_CARRIER",
            preferred_date=date.today() + timedelta(days=30),
        )
        # TRP-03: an external carrier is doing this one, so their own
        # offer is on file before the departure is organized.
        from apps.transport.services import declare_carrier_price

        declare_carrier_price(
            order, user=self.manager, carrier_name="Carrier Ltd",
            price=Decimal("800.00"),
        )
        self.case.status = self.statuses["advance_paid"]
        self.case.save()

        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        advance = AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("400")
        )
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "mark_advance_paid").done)
        self.assertFalse(self.transition(guide, "departure_scheduled").enabled)

        mark_advance_paid(advance, self.manager)
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "mark_advance_paid").done)
        self.assertTrue(self.transition(guide, "departure_scheduled").enabled)
        self._do_transition(self.case, "departure_scheduled")  # guard parity

    def test_in_transit_delivery_protocol_gates_delivered(self):
        # TRP-08: the delivery protocol blocks the `delivered` transition;
        # the signed-scan step stays advisory.
        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot", mode="OWN_VEHICLE"
        )
        self.case.status = self.statuses["in_transit"]
        self.case.save()

        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "confirm_delivery").done)
        self.assertFalse(self.transition(guide, "delivered").enabled)

        from apps.transport.services import record_delivery_confirmation

        record_delivery_confirmation(
            self.case,
            user=self.manager,
            attendee_name="Pat Owner",
            unloading_done=True,
            put_into_service=True,
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "confirm_delivery").done)
        self.assertFalse(self.step(guide, "delivery_protocol_scan").done)
        self.assertTrue(self.transition(guide, "delivered").enabled)
        self._do_transition(self.case, "delivered")  # guard parity




def _service_type(name="Storage"):
    """WNT-02 (§5.2): the contracted guard - and so the checklist that
    mirrors it - wants at least one selected service."""
    StorageServiceType = django_apps.get_model(
        "storage", "StorageServiceType"
    )
    service_type, _created = StorageServiceType.objects.get_or_create(name=name)
    return service_type

class WinterStorageGuideTests(GuideTestBase):
    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.STORAGE, STORAGE_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            raw_phone="+44 67 1",
        )

    def test_asset_link_gates_contracted(self):
        from apps.storage.services import upsert_agreement

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        # WNT-01/WNT-03: docs and pricing basis recorded, so the linked
        # asset is the only thing still holding `contracted` back.
        upsert_agreement(
            self.case,
            permit_no="MNE-1",
            asset_length_m=Decimal("11.50"),
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
            accommodation="OPEN",
            term="SEASONAL",
        )
        self.case.refresh_from_db()
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "link_asset").done)
        self.assertFalse(self.transition(guide, "contracted").enabled)

        client = Client.objects.create(name="Mark")
        asset = Asset.objects.create(
            client=client,
            name="Unit 7",
            asset_type="Generator",
            length_m=Decimal("11.50"),
        )
        self.case.client = client
        self.case.asset = asset
        self.case.save()
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "link_asset").done)
        self.assertTrue(self.transition(guide, "contracted").enabled)
        self._do_transition(self.case, "contracted")  # guard parity

    def test_estimate_sent_requires_signed_intake_only(self):
        # WNT-03 reorder: the signed-intake checklist gates
        # estimate_sent -> asset_received (it used to sit on contracted).
        from apps.intake.services import (
            confirm_no_intake_items,
            create_intake_record,
            sign_intake_record,
        )

        self.case.status = self.statuses["estimate_sent"]
        self.case.save()
        # §7.2: this class starts with no asset on purpose (see
        # test_asset_link_gates_contracted), and an intake record cannot be
        # signed for an asset that is not in the registry.
        link_identified_asset(self.case)

        guide = self.guide(self.case)
        keys = [s.key for s in guide["steps"]]
        self.assertNotIn("create_work_order", keys)  # no SERVICE steps leak
        self.assertFalse(self.transition(guide, "asset_received").enabled)

        record = create_intake_record(
            self.case, self.manager, mount_status="ON_MOUNT",
            cleanliness_status="CLEAN", damage_note="none",
            energy_level="HALF",  # §7.6
        )
        # SVC-07 applies to storage intake too (WNT-05).
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings

        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            attach_file(
                self.case,
                SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
                Attachment.Kind.PHOTO,
                Attachment.Stage.INTAKE,
                user=self.manager,
            )
        confirm_no_intake_items(record, self.manager)  # SVC-06
        sign_intake_record(record, self.manager)
        guide = self.guide(self.case)
        self.assertTrue(self.transition(guide, "asset_received").enabled)
        self._do_transition(self.case, "asset_received")  # guard parity


class GuideRenderTests(GuideTestBase):
    """The wizard renders on the detail page and preserves the old cards."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["new_report"],
            location="Customer site",  # §7.3
            # §7.1: designated AND reachable - the guard asks that the
            # designation still resolves to somebody. Answered through a
            # linked client rather than raw_* fields, which would make
            # the case look like an untriaged portal submission and
            # change which step the hero picks.
            client=_reachable_client(),
            contact_role=ContactRole.OWNER,
        )
        self.client.force_login(self.manager)

    def _get(self):
        from django.urls import reverse

        return self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )

    def test_stepper_hero_and_sections_render(self):
        response = self._get()
        self.assertContains(response, "guide-stepper")
        self.assertContains(response, "Next step")
        self.assertContains(
            response, "Book an intake appointment with the client"
        )
        self.assertContains(response, "guide-section-toggle")
        # The hero's anchor section is pre-opened.
        self.assertContains(response, 'id="acc-appointments" class="collapse show"')

    def test_old_cards_still_present_inside_sections(self):
        response = self._get()
        self.assertContains(response, "Appointments")
        self.assertContains(response, "Add activity log entry")
        self.assertContains(response, "Status history")
        # Disabled transition: not offered as an enabled submit in the hero,
        # but the manual Transitions card still lists it.
        self.assertContains(response, 'name="to_status" value="intake_scheduled"')

    def test_focus_mode_hides_unrelated_sections(self):
        response = self._get()
        # new_report only needs the appointments section - it stays primary...
        self.assertNotContains(
            response,
            'guide-section guide-section-secondary" id="section-acc-appointments"',
        )
        # ...while unrelated sections are hidden behind the toggle.
        self.assertContains(
            response,
            'guide-section guide-section-secondary" id="section-acc-quotes"',
        )
        self.assertContains(response, "Show all sections")

    def test_phase_groups_render_with_current_accent(self):
        response = self._get()
        # Work-zone phase groups with workflow-phase headings.
        self.assertContains(response, 'id="phase-intake"')
        self.assertContains(response, 'id="phase-approval"')
        self.assertContains(response, "Scheduling and intake")
        self.assertContains(response, "Handover and closing")
        # new_report's hero (book appointment) lives in the intake phase.
        self.assertContains(
            response, 'guide-phase guide-phase-current" id="phase-intake"'
        )
        self.assertNotContains(
            response, 'guide-phase guide-phase-current" id="phase-approval"'
        )
        # The split work-order card: execution and QC are own sections.
        self.assertContains(response, 'id="section-acc-work-execution"')
        self.assertContains(response, 'id="section-acc-quality"')
        # The hero "Do it" link defers collapse handling to the page JS.
        self.assertContains(response, 'data-guide-open="acc-appointments"')

    def test_command_bar_and_case_file_zone(self):
        response = self._get()
        self.assertContains(response, "guide-commandbar")
        self.assertContains(response, "Case file")

    def test_done_steps_fold_into_summary(self):
        from apps.scheduling.services import book_appointment
        from apps.scheduling.workinghours import next_working_slot

        # A working-hours slot, not a bare "tomorrow": SVC-04 refuses a
        # time the yard is shut, which would otherwise make this test
        # depend on the day it runs.
        book_appointment(
            self.case,
            "INTAKE",
            next_working_slot(),
            user=self.manager,
        )
        response = self._get()
        self.assertContains(response, "guide-done")
        self.assertContains(response, "Completed")
        self.assertContains(response, "Book an intake appointment with the client")

    def test_waiting_state_renders_for_technician(self):
        from decimal import Decimal as D

        from apps.quotes.services import create_quote, record_approval, send_quote

        django_apps.get_model("accounts", "ApprovalLimit").objects.create(
            role="MANAGER", max_amount=D("1000")
        )
        restricted = StatusTransition.objects.get(
            from_status=self.statuses["awaiting_approval"],
            to_status=self.statuses["in_progress"],
        )
        restricted.allowed_roles = ["MANAGER", "DIRECTOR"]
        restricted.save()
        self.case.status = self.statuses["awaiting_approval"]
        self.case.save()
        quote = create_quote(self.case, "Popravka", D("500"))
        send_quote(quote, self.manager)
        record_approval(quote, "client", "approved", user=self.manager)
        record_approval(quote, "manager", "approved", user=self.manager)

        self.client.force_login(self.technician)
        response = self._get()
        self.assertContains(response, "Waiting for")


class WinterStorageInquiryGuideTests(GuideTestBase):
    """WNT-01/WNT-03: the documentation step gates `contracted` in the guide
    the same way the transition guard does (guide/guard parity). The priced
    estimate follows contracting, so there is no pre-contract quote_sent."""

    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.STORAGE, STORAGE_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            raw_phone="+44 67 1",
        )

    def test_documentation_gates_contracted(self):
        from apps.storage.services import upsert_agreement

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        guide = self.guide(self.case)
        step = self.step(guide, "record_documentation")
        self.assertFalse(step.done)
        self.assertEqual(step.anchor, "acc-storage-docs")
        self.assertFalse(self.transition(guide, "contracted").enabled)

        upsert_agreement(
            self.case,
            permit_no="MNE-1",
            asset_length_m=Decimal("8.50"),
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
            accommodation="OPEN",
            term="SEASONAL",
        )
        client = Client.objects.create(name="Mark")
        self.case.client = client
        self.case.asset = Asset.objects.create(
            client=client,
            name="Unit 7",
            asset_type="Generator",
            length_m=Decimal("8.50"),
        )
        self.case.save()
        self.case.refresh_from_db()
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "record_documentation").done)
        self.assertTrue(self.step(guide, "record_pricing_basis").done)
        self.assertTrue(self.transition(guide, "contracted").enabled)
        self._do_transition(self.case, "contracted")  # guard parity

    def test_database_entry_step_mirrors_the_guard(self):
        """WNT-02 (§5.2): a blocking checklist item the wizard shows as
        outstanding beats a transition that looks available and refuses."""
        from apps.storage.services import upsert_agreement

        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")

        upsert_agreement(
            self.case,
            permit_no="MNE-1",
            asset_length_m=Decimal("8.50"),
            accommodation="OPEN",
            term="SEASONAL",
        )
        client = Client.objects.create(name="Mark")
        self.case.client = client
        self.case.asset = Asset.objects.create(
            client=client, name="Unit 7", length_m=Decimal("8.50")
        )
        self.case.save()
        self.case.refresh_from_db()

        guide = self.guide(self.case)
        step = self.step(guide, "record_database_entry")
        self.assertFalse(step.done)
        self.assertFalse(self.transition(guide, "contracted").enabled)
        # And the permit document is advice, not a gate.
        self.assertFalse(self.step(guide, "file_permit").blocking)

        upsert_agreement(
            self.case,
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[_service_type()],
        )
        self.case.refresh_from_db()
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "record_database_entry").done)
        self.assertTrue(self.transition(guide, "contracted").enabled)
        self._do_transition(self.case, "contracted")  # guard parity

    def test_link_asset_step_targets_card_not_admin(self):
        guide = self.guide(self.case)
        step = self.step(guide, "link_asset")
        self.assertEqual(step.anchor, "acc-asset-link")
        self.assertIsNone(step.admin_url)

    def test_position_step_gates_in_storage(self):
        from apps.storage.services import assign_position, upsert_agreement

        self.case.status = self.statuses["asset_received"]
        self.case.save()
        guide = self.guide(self.case)
        step = self.step(guide, "assign_position")
        self.assertFalse(step.done)
        # WNT-05: ``_guard_in_storage`` refuses the move without a placement.
        self.assertFalse(self.transition(guide, "in_storage").enabled)

        agreement = upsert_agreement(self.case, asset_length_m=Decimal("8.5"))
        assign_position(agreement, "H1", "A", 1)
        self.case.refresh_from_db()
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "assign_position").done)
        self.assertTrue(self.transition(guide, "in_storage").enabled)
        self._do_transition(self.case, "in_storage")  # guard parity
        # Once in storage the placement is a record, not a gate.
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "assign_position").blocking)


class OnSiteWorkGuideTests(GuideTestBase):
    """SVC-03: the checklist drops the steps an on-site job cannot do.

    A step that does not apply is removed rather than shown as done -
    ticking off an intake record that will never be written is a lie in the
    other direction.
    """

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["new_report"],
            location="Customer site",  # §7.3
            # §7.1: designated AND reachable - the guard asks that the
            # designation still resolves to somebody. Answered through a
            # linked client rather than raw_* fields, which would make
            # the case look like an untriaged portal submission and
            # change which step the hero picks.
            client=_reachable_client(),
            contact_role=ContactRole.OWNER,
        )

    def keys(self, case, user=None):
        return [s.key for s in self.guide(case, user)["steps"]]

    def test_consult_step_is_advisory_and_offered_first(self):
        guide = self.guide(self.case)
        consult = self.step(guide, "work_location_consult")
        self.assertFalse(consult.done)
        self.assertFalse(consult.blocking)  # SVC-03 says "may"
        # ...so it never blocks the transition on its own.
        self.assertFalse(self.transition(guide, "intake_scheduled").enabled)

        record_work_location_consult(
            self.case, WorkLocation.ON_SITE, self.manager
        )
        self.assertTrue(self.step(self.guide(self.case), "work_location_consult").done)

    def test_on_site_case_is_asked_for_a_visit_not_a_delivery(self):
        record_work_location_consult(
            self.case, WorkLocation.IN_FIELD, self.manager
        )
        keys = self.keys(self.case)
        self.assertIn("book_on_site_visit", keys)
        self.assertNotIn("book_intake_appointment", keys)

        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.ON_SITE_VISIT,
            scheduled_for=next_working_slot(),
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "book_on_site_visit").done)
        self.assertTrue(self.transition(guide, "intake_scheduled").enabled)
        self._do_transition(self.case, "intake_scheduled")  # guard parity

    def test_yard_case_keeps_the_delivery_step(self):
        record_work_location_consult(
            self.case, WorkLocation.SERVICE_CENTER, self.manager
        )
        keys = self.keys(self.case)
        self.assertIn("book_intake_appointment", keys)
        self.assertNotIn("book_on_site_visit", keys)

    def test_intake_steps_disappear_on_an_on_site_case(self):
        record_work_location_consult(
            self.case, WorkLocation.ON_SITE, self.manager
        )
        self.case.status = self.statuses["intake_scheduled"]
        self.case.save()
        keys = self.keys(self.case)
        for dropped in (
            "create_intake_record",
            "sign_intake_record",
            "upload_arrival_photos",
        ):
            self.assertNotIn(dropped, keys)
        # The photo survives - only its framing changes.
        self.assertIn("upload_visit_photos", keys)
        # The work-order records are untouched.
        self.assertIn("sign_work_order", keys)

    def test_handover_steps_disappear_on_an_on_site_case(self):
        record_work_location_consult(
            self.case, WorkLocation.ON_SITE, self.manager
        )
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()
        keys = self.keys(self.case)
        for dropped in (
            "create_handover",
            "handover_tech_sign",
            "handover_client_scan",
            "sign_handover",
        ):
            self.assertNotIn(dropped, keys)
        # Settlement is still the gate on closing.
        self.assertIn("mark_settled", keys)

    def test_blank_work_location_keeps_every_step(self):
        """Regression: a case that never had the consult behaves as
        it always did."""
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()
        keys = self.keys(self.case)
        self.assertIn("create_handover", keys)
        self.assertIn("sign_handover", keys)
