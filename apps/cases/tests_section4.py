"""Procedure §4 "Carrying out the work, inspection and handover".

Four of the five sub-steps were already built. What was missing was the
qualifier in each sentence rather than the record itself:

* §4.1 - "according to the **approved** order". Work could be logged against a
  preliminary order nobody had endorsed, which SVC-02 made reachable
  across the whole lifecycle by opening one at triage.
* §4.2 - the procedure names two controls (the asset is put into service, or an
  appropriate test run is performed) and two findings (the fault is
  cleared, the asset is safe and functional). One PASS carried all four.
* §4.3 - the "final cost" the client is told was a hand-typed number
  that nothing compared against the work order.
* §4.5 - "The work order **and** the service ticket are closed". Only the ticket
  ever closed; the order stayed writable forever.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.cases.models import WorkLocation
from apps.cases.tests import WorkflowTestBase, build_service_workflow
from apps.parts.models import Part
from apps.scheduling.workinghours import next_working_slot
from apps.service.models import ServicePriceItem, TestRun, WorkOrder
from apps.service.services import (
    close_work_order,
    log_work,
    record_test_run,
    record_used_part,
    reopen_work_order,
    send_completion_notice,
)


def _labour_rate(price="50"):
    return ServicePriceItem.objects.create(
        category=ServicePriceItem.Category.LABOR,
        name="Radni sat",
        price=Decimal(price),
        unit=ServicePriceItem.Unit.PER_HOUR,
    )


class EndorsedOrderTests(WorkflowTestBase):
    """§4.1: the work is done against an order somebody endorsed."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "in_progress")
        self.work_order = WorkOrder.objects.create(case=self.case)

    def _part(self):
        return Part.objects.create(
            code="P-1", name="Impeller", is_consumable=True, stock_qty=3
        )

    def test_work_is_refused_on_a_preliminary_order(self):
        with self.assertRaisesMessage(ValidationError, "still preliminary"):
            log_work(
                self.work_order, self.user, description="x", hours=Decimal("1")
            )

    def test_parts_are_refused_on_a_preliminary_order(self):
        with self.assertRaisesMessage(ValidationError, "still preliminary"):
            record_used_part(
                self.work_order, self.user, part=self._part(), quantity=1
            )

    def test_both_are_accepted_once_it_is_endorsed(self):
        from apps.service.services import finalize_work_order

        finalize_work_order(self.work_order, self.user)
        entry = log_work(
            self.work_order, self.user, description="x", hours=Decimal("1")
        )
        used = record_used_part(
            self.work_order, self.user, part=self._part(), quantity=2
        )
        self.assertIsNotNone(entry.pk)
        self.assertEqual(used.quantity, 2)

    def test_the_refusal_names_the_rule(self):
        """The technician has to know which order is being waited on."""
        with self.assertRaisesMessage(ValidationError, "SVC-15"):
            log_work(
                self.work_order, self.user, description="x", hours=Decimal("1")
            )


class WorkOrderClosingTests(WorkflowTestBase):
    """§4.5: "The work order and the service ticket are closed..." - both."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        # In the water, so no handover record is owed (SVC-03); financial
        # settlement is the other half of the closing guard.
        self.case = self._make_case(
            self.statuses,
            "ready_for_handover",
            work_location=WorkLocation.ON_SITE,
            financially_settled=True,
        )
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )

    def test_closing_the_case_closes_the_order(self):
        self._transition(self.case, "closed")
        self.work_order.refresh_from_db()
        self.assertIsNotNone(self.work_order.closed_at)
        self.assertEqual(self.work_order.closed_by, self.user)

    def test_a_closed_order_takes_no_work(self):
        self._transition(self.case, "closed")
        self.work_order.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "takes no further"):
            log_work(
                self.work_order, self.user, description="late", hours=Decimal("2")
            )

    def test_a_closed_order_takes_no_parts(self):
        self._transition(self.case, "closed")
        self.work_order.refresh_from_db()
        part = Part.objects.create(
            code="P-2", name="Filter", is_consumable=True, stock_qty=1
        )
        with self.assertRaisesMessage(ValidationError, "takes no further"):
            record_used_part(
                self.work_order, self.user, part=part, quantity=1
            )

    def test_closing_twice_keeps_the_first_stamp(self):
        """A reopened and re-closed case must not lose the timestamp that
        matches the handover the client signed."""
        close_work_order(self.work_order, self.user)
        first = self.work_order.closed_at
        close_work_order(self.work_order, self.user)
        self.work_order.refresh_from_db()
        self.assertEqual(self.work_order.closed_at, first)

    def test_a_case_without_a_work_order_closes_normally(self):
        """Only SERVICE cases carry one; closing must not depend on it.

        A separate case, not this one with its order deleted: deleting the
        row leaves the reverse relation cached on the instance, so that
        version of the test would pass a stale object to the hook and
        prove nothing about a case that never had an order.
        """
        other = self._make_case(
            self.statuses,
            "ready_for_handover",
            work_location=WorkLocation.ON_SITE,
            financially_settled=True,
        )
        case = self._transition(other, "closed")
        self.assertEqual(case.status.code, "closed")

    def test_reopening_needs_a_reason(self):
        manager = self._manager()
        close_work_order(self.work_order, self.user)
        with self.assertRaisesMessage(ValidationError, "State why"):
            reopen_work_order(self.work_order, manager, reason="   ")

    def test_reopening_needs_the_role(self):
        close_work_order(self.work_order, self.user)
        with self.assertRaisesMessage(ValidationError, "may not reopen"):
            reopen_work_order(
                self.work_order, self.user, reason="wrong part billed"
            )

    def test_reopening_restores_writing_and_logs_both_acts(self):
        from apps.cases.models import ActivityLog

        manager = self._manager()
        close_work_order(self.work_order, self.user)
        reopen_work_order(
            self.work_order, manager, reason="Impeller billed to the wrong order"
        )
        self.work_order.refresh_from_db()
        self.assertIsNone(self.work_order.closed_at)
        self.assertIsNone(self.work_order.closed_by)
        entry = log_work(
            self.work_order, manager, description="fix", hours=Decimal("1")
        )
        self.assertIsNotNone(entry.pk)
        texts = [
            log.text
            for log in ActivityLog.objects.filter(case=self.case)
        ]
        self.assertTrue(any("closed with the case" in text for text in texts))
        self.assertTrue(
            any("wrong order" in text for text in texts),
            "the reason for reopening has to survive in the log",
        )

    def test_reopening_an_open_order_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "is not closed"):
            reopen_work_order(self.work_order, self._manager(), reason="x")

    def _manager(self):
        User = get_user_model()
        return User.objects.create_user(
            username="mgr-s4", password="demo12345", role="MANAGER"
        )


class QualityControlTests(WorkflowTestBase):
    """§4.2: which control was performed, and was the fault cleared."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "quality_check")
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )

    def _record(self, **kwargs):
        kwargs.setdefault("kind", TestRun.Kind.TEST_RUN)
        kwargs.setdefault("result", TestRun.Result.PASSED)
        kwargs.setdefault("fault_cleared", True)
        result = kwargs.pop("result")
        with self.captureOnCommitCallbacks(execute=True):
            return record_test_run(self.work_order, result, self.user, **kwargs)

    def test_the_control_performed_is_required(self):
        with self.assertRaisesMessage(ValidationError, "which control"):
            self._record(kind="")

    def test_a_field_test_is_refused_when_the_asset_never_left_the_water(self):
        self.case.work_location = WorkLocation.ON_SITE
        self.case.save()
        with self.assertRaisesMessage(ValidationError, "never lifted out"):
            self._record(kind=TestRun.Kind.FIELD_TEST)

    def test_a_test_run_is_available_on_an_in_water_case(self):
        self.case.work_location = WorkLocation.ON_SITE
        self.case.save()
        run = self._record(kind=TestRun.Kind.TEST_RUN)
        self.assertEqual(run.kind, TestRun.Kind.TEST_RUN)

    def test_a_field_test_is_fine_on_a_yard_case(self):
        run = self._record(kind=TestRun.Kind.FIELD_TEST)
        self.assertEqual(run.kind, TestRun.Kind.FIELD_TEST)

    def test_a_pass_requires_the_fault_to_be_cleared(self):
        """An engine can run perfectly and still not be the reason the
        client came in."""
        with self.assertRaisesMessage(ValidationError, "reported fault"):
            self._record(fault_cleared=False)

    def test_an_unanswered_fault_question_cannot_pass_either(self):
        with self.assertRaisesMessage(ValidationError, "reported fault"):
            self._record(fault_cleared=None)

    def test_a_failed_run_may_leave_the_fault_uncleared(self):
        run = self._record(result=TestRun.Result.FAIL, fault_cleared=False)
        self.assertEqual(run.result, TestRun.Result.FAIL)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "in_progress")

    def test_the_log_names_the_control_and_the_finding(self):
        from apps.cases.models import ActivityLog

        run = self._record(kind=TestRun.Kind.FIELD_TEST)
        entry = ActivityLog.objects.filter(
            case=self.case, text__contains="SVC-16"
        ).latest("pk")
        self.assertIn(str(run.pk), entry.text)
        self.assertIn("operating conditions", entry.text.lower())

    def test_legacy_runs_keep_their_blank_answers(self):
        """Rows written before the fields existed are not retro-filled -
        nobody made those statements."""
        legacy = TestRun.objects.create(
            work_order=self.work_order, result=TestRun.Result.PASSED
        )
        self.assertEqual(legacy.kind, "")
        self.assertIsNone(legacy.fault_cleared)


class FinalCostTests(WorkflowTestBase):
    """§4.3: the client is told a final cost the work order supports."""

    def setUp(self):
        super().setUp()
        from apps.clients.models import Client

        self.statuses = build_service_workflow()
        self.client_record = Client.objects.create(
            name="Owner", email="owner@example.com"
        )
        self.case = self._make_case(
            self.statuses, "ready_for_handover", client=self.client_record
        )
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )
        _labour_rate()
        log_work(
            self.work_order, self.user, description="Work", hours=Decimal("4")
        )  # 4 x 50 = 200

    def _send(self, amount, **kwargs):
        kwargs.setdefault("works_performed", "Impeller replaced")
        kwargs.setdefault("handover_mode", "PICKUP")
        # A slot inside opening hours: "now + 2 days" keeps the wall-clock
        # time, so a suite run in the evening or on a Sunday tripped the
        # SVC-17 working-hours guard instead of the one under test.
        kwargs.setdefault("scheduled_for", next_working_slot())
        kwargs.setdefault("channel", "EMAIL")
        return send_completion_notice(
            self.case, self.user, final_cost=Decimal(amount), **kwargs
        )

    def test_the_recorded_total_needs_no_explanation(self):
        notice = self._send("200")
        self.assertEqual(notice.final_cost, Decimal("200"))
        self.assertEqual(notice.cost_note, "")

    def test_a_different_figure_without_a_reason_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "records 200.00"):
            self._send("1")

    def test_the_refusal_names_what_the_client_would_be_told(self):
        with self.assertRaisesMessage(ValidationError, "told 1"):
            self._send("1")

    def test_a_reason_lets_a_different_figure_through(self):
        notice = self._send("150", cost_note="Goodwill discount, agreed by phone")
        self.assertEqual(notice.final_cost, Decimal("150"))
        self.assertIn("Goodwill", notice.cost_note)

    def test_the_reason_is_written_to_the_case_log(self):
        from apps.cases.models import ActivityLog

        self._send("150", cost_note="Goodwill discount")
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="Goodwill discount"
            ).exists()
        )

    def test_an_untotalled_order_asks_for_the_reason_instead_of_refusing(self):
        """Unlike the SVC-14 guard, this one must not leave the client
        uninformed about finished work - it demands the reason instead."""
        part = Part.objects.create(
            code="NP-1", name="No price", is_consumable=False, stock_qty=1
        )
        record_used_part(self.work_order, self.user, part=part, quantity=1)
        with self.assertRaisesMessage(ValidationError, "cannot be totalled"):
            self._send("300")
        notice = self._send("300", cost_note="Part invoiced at 100 EUR by the supplier")
        self.assertEqual(notice.final_cost, Decimal("300"))

    def test_the_untotalled_refusal_names_the_unpriced_part(self):
        part = Part.objects.create(
            code="NP-2", name="No price", is_consumable=False, stock_qty=1
        )
        record_used_part(self.work_order, self.user, part=part, quantity=1)
        with self.assertRaisesMessage(ValidationError, "NP-2"):
            self._send("300")
