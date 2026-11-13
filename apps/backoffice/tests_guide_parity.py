"""Guide/guard parity regressions from the 2026-11-04 code review.

Each test drives the same situation through the guide (``enabled``) and
through ``transition_case`` (refused / allowed), so a predicate can no
longer tick a step the guard is about to refuse.
"""

from datetime import timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.utils import timezone

from apps.accounts.models import ApprovalLimit
from apps.cases.exceptions import GuardCheckFailed
from apps.cases.models import Case, CaseType, ReportChannel, StatusHistory
from apps.cases.services import transition_case
from apps.cases.tests import build_service_workflow, build_workflow

from .tests_guide import (
    STORAGE_CODES,
    TRANSPORT_CODES,
    GuideTestBase,
    _reachable_client,
)


class ParityBase(GuideTestBase):
    def assert_refused(self, case, code, contains=None):
        """``contains`` names the guard expected to fire (e.g. "TRP-07");
        without it an earlier guard in the chain would satisfy the test
        for the wrong reason."""
        guide = self.guide(case)
        self.assertFalse(self.transition(guide, code).enabled)
        if contains is None:
            with self.assertRaises(GuardCheckFailed):
                transition_case(case, code, self.manager)
        else:
            with self.assertRaisesMessage(GuardCheckFailed, contains):
                transition_case(case, code, self.manager)

    def enter(self, case, code, when=None):
        """Put ``case`` in status ``code`` with a history row dated ``when``."""
        history = StatusHistory.objects.create(
            case=case,
            from_status=case.status,
            to_status=self.statuses[code],
            changed_by=self.manager,
        )
        if when is not None:
            StatusHistory.objects.filter(pk=history.pk).update(changed_at=when)
        case.status = self.statuses[code]
        case.save(update_fields=["status"])


class ServiceParityTests(ParityBase):
    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["in_progress"],
            location="Customer site",
            client=_reachable_client(),
        )

    def test_a_pass_before_rework_does_not_count(self):
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        TestRun = django_apps.get_model("service", "TestRun")
        work_order = WorkOrder.objects.create(case=self.case)
        old_pass = TestRun.objects.create(work_order=work_order, result="PASS")
        TestRun.objects.filter(pk=old_pass.pk).update(
            performed_at=timezone.now() - timedelta(days=2)
        )
        # Rework happened; the case is back in quality control today.
        self.enter(self.case, "quality_check", when=timezone.now() - timedelta(hours=1))

        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "record_pass_test_run").done)
        self.assert_refused(self.case, "ready_for_handover")

        TestRun.objects.create(work_order=work_order, result="PASS")
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "record_pass_test_run").done)
        self.assertTrue(self.transition(guide, "ready_for_handover").enabled)
        self._do_transition(self.case, "ready_for_handover")

    def test_awaiting_part_still_needs_the_accepted_quote(self):
        Quote = django_apps.get_model("quotes", "Quote")
        self.enter(self.case, "awaiting_part")
        quote = Quote.objects.create(
            case=self.case, description="Pump", amount=Decimal("50"),
            status=Quote.Status.DRAFT,
        )
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "send_quote").done)
        self.assert_refused(self.case, "in_progress")

        ApprovalLimit.objects.create(role="TECHNICIAN", max_amount=Decimal("1000"))
        Approval = django_apps.get_model("quotes", "Approval")
        quote.status = Quote.Status.ACCEPTED
        quote.save(update_fields=["status"])
        Approval.objects.create(
            quote=quote, level="client", decision="approved", decided_at=timezone.now()
        )
        guide = self.guide(self.case)
        self.assertTrue(self.transition(guide, "in_progress").enabled)
        self._do_transition(self.case, "in_progress")


class WinterStorageParityTests(ParityBase):
    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.STORAGE, STORAGE_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            raw_phone="+44 67 1",
        )

    def test_an_offer_sent_before_contracting_is_not_the_estimate(self):
        Quote = django_apps.get_model("quotes", "Quote")
        early = Quote.objects.create(
            case=self.case, description="Early", amount=Decimal("900"),
            status=Quote.Status.SENT,
        )
        Quote.objects.filter(pk=early.pk).update(
            created_at=timezone.now() - timedelta(days=1)
        )
        self.enter(self.case, "contracted", when=timezone.now() - timedelta(hours=1))

        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "create_estimate").done)
        self.assertFalse(self.step(guide, "send_estimate").done)
        self.assert_refused(self.case, "estimate_sent")

        Quote.objects.create(
            case=self.case, description="Estimate", amount=Decimal("950"),
            status=Quote.Status.SENT,
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "send_estimate").done)
        self.assertTrue(self.transition(guide, "estimate_sent").enabled)
        self._do_transition(self.case, "estimate_sent")


class TransportParityTests(ParityBase):
    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["advance_paid"],
        )

    def test_departure_needs_a_date(self):
        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("400"), paid_at=timezone.now()
        )
        guide = self.guide(self.case)
        self.assertFalse(self.step(guide, "book_departure").done)
        self.assert_refused(self.case, "departure_scheduled", contains="TRP-05")

        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot", mode="OWN_VEHICLE",
            preferred_date=timezone.localdate() + timedelta(days=30),
        )
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "book_departure").done)

    def test_loading_counts_only_when_signed_ready(self):
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        LoadingConfirmation = django_apps.get_model("transport", "LoadingConfirmation")
        LoadingPlan = django_apps.get_model("transport", "LoadingPlan")
        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        self.enter(self.case, "departure_scheduled")
        order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot", mode="OWN_VEHICLE",
        )
        # TRP-06 and TRP-02 are satisfied so that TRP-07 is the only guard
        # left to refuse - otherwise the refusal proves nothing about loading.
        IntakeRecord.objects.create(
            case=self.case, received_by=self.manager, signed_at=timezone.now()
        )
        LoadingPlan.objects.create(
            transport_order=order,
            unloading_location="Depot B, bay C",
            lifting_availability=LoadingPlan.CraneAvailability.AVAILABLE,
            attendee="Site manager",
            loading_at=timezone.now() + timedelta(days=1),
            unloading_at=timezone.now() + timedelta(days=2),
        )
        confirmation = LoadingConfirmation.objects.create(
            transport_order=order, readiness=LoadingConfirmation.Readiness.NOT_READY
        )
        guide = self.guide(self.case)
        step = self.step(guide, "confirm_loading")
        self.assertFalse(step.done)
        self.assertTrue(step.blocking)
        self.assert_refused(self.case, "in_transit", contains="TRP-07")

        confirmation.readiness = LoadingConfirmation.Readiness.READY
        confirmation.signed_at = timezone.now()
        confirmation.save()
        # The TRP-07 guard cached the order (and its NOT_READY
        # confirmation) on this instance; read the stored state.
        self.case.refresh_from_db()
        guide = self.guide(self.case)
        self.assertTrue(self.step(guide, "confirm_loading").done)
