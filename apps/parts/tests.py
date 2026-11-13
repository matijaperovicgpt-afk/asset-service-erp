"""Tests for the parts app.

Workflow guard tests (SVC-14 over part orders) live in the cases app;
the ``order_part`` service (SVC-11 / INV-05) is tested here.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel, Status
from apps.quotes.models import AdvancePayment

from .models import Part, PartOrder
from .services import is_stock_part, order_part, receive_part_order


class PartsSmokeTest(SimpleTestCase):
    def test_models_import(self):
        from . import models

        self.assertTrue(hasattr(models, "Part"))
        self.assertTrue(hasattr(models, "PartOrder"))


class OrderPartTests(TestCase):
    """SVC-11 / INV-05: non-stock parts require a 50% advance payment."""

    def setUp(self):
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.stock_part = Part.objects.create(
            code="IMP-1", name="Impeller", is_consumable=True, stock_qty=5
        )
        self.rare_part = Part.objects.create(
            code="GBX-9", name="Gearbox", is_consumable=False, stock_qty=0
        )

    def test_is_stock_part(self):
        self.assertTrue(is_stock_part(self.stock_part))
        self.assertFalse(is_stock_part(self.rare_part))
        # A consumable that ran out of stock is not "in stock".
        self.stock_part.stock_qty = 0
        self.assertFalse(is_stock_part(self.stock_part))

    def test_stock_part_orders_without_advance(self):
        order = order_part(self.case, self.stock_part)
        self.assertIsNone(order.advance_payment)

    def test_non_stock_part_requires_advance(self):
        with self.assertRaises(ValidationError):
            order_part(self.case, self.rare_part)

    def test_advance_must_belong_to_same_case(self):
        other_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.case.status,
        )
        advance = AdvancePayment.objects.create(
            case=other_case, percent=50, amount=Decimal("400")
        )
        with self.assertRaises(ValidationError):
            order_part(self.case, self.rare_part, advance_payment=advance)

    def test_advance_must_cover_at_least_half(self):
        advance = AdvancePayment.objects.create(
            case=self.case, percent=30, amount=Decimal("100")
        )
        with self.assertRaises(ValidationError):
            order_part(self.case, self.rare_part, advance_payment=advance)

    def test_non_stock_part_with_valid_advance_links_it(self):
        advance = AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("400")
        )
        order = order_part(self.case, self.rare_part, advance_payment=advance)
        self.assertEqual(order.advance_payment, advance)
        self.assertEqual(PartOrder.objects.count(), 1)


class ReceivePartOrderTests(TestCase):
    """Receiving a part: stock bump + SYSTEM log; no case transition."""

    def setUp(self):
        self.status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_part",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
        )
        self.part = Part.objects.create(
            code="IMP-1", name="Impeller", is_consumable=True, stock_qty=5
        )
        self.order = order_part(self.case, self.part)

    def test_receive_bumps_stock_and_stamps_received_at(self):
        receive_part_order(self.order)
        self.order.refresh_from_db()
        self.part.refresh_from_db()
        self.assertIsNotNone(self.order.received_at)
        self.assertEqual(self.part.stock_qty, 6)

    def test_receive_writes_system_activity_log(self):
        receive_part_order(self.order)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn(
            "Part order #%s (%s) received." % (self.order.pk, self.part.code),
            entry.text,
        )
        self.assertNotIn("still open", entry.text)

    def test_log_mentions_other_open_orders(self):
        order_part(self.case, self.part)  # second order, stays open
        receive_part_order(self.order)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertIn("1 other part order(s) on this case are still open", entry.text)

    def test_receive_does_not_transition_the_case(self):
        receive_part_order(self.order)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.status)

    def test_double_receive_is_refused(self):
        receive_part_order(self.order)
        self.order.refresh_from_db()
        self.part.refresh_from_db()
        with self.assertRaises(ValidationError):
            receive_part_order(self.order)
        self.part.refresh_from_db()
        self.assertEqual(self.part.stock_qty, 6)
        self.assertEqual(ActivityLog.objects.filter(case=self.case).count(), 1)
