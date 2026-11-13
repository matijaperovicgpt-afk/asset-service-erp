"""Phase E of the 2026-11-04 code review: the SVC-10 / INV-05 rules that
lived in the backoffice views now live here and are tested directly."""

from decimal import Decimal

from django.contrib.auth import get_user_model

from apps.cases.models import ActivityLog
from apps.cases.tests import WorkflowTestBase, build_service_workflow

from .models import Part
from .services import (
    create_catalogue_part,
    may_stock_parts,
    order_part_with_advance,
    part_advance_amount,
)


class CataloguePartRuleTests(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.case = self._make_case(build_service_workflow(), "new_report")
        User = get_user_model()
        self.technician = User.objects.create_user(
            username="tech", password="x", role="TECHNICIAN"
        )
        self.manager = User.objects.create_user(
            username="mgr", password="x", role="MANAGER"
        )

    def _add(self, user, **overrides):
        values = dict(
            code="IMP-1", name="Impeller", is_consumable=True, stock_qty=5,
            unit_price=Decimal("12.50"),
        )
        values.update(overrides)
        return create_catalogue_part(self.case, user, **values)

    def test_only_stocking_roles_may_declare_a_stocked_consumable(self):
        self.assertFalse(may_stock_parts(self.technician))
        self.assertTrue(may_stock_parts(self.manager))
        part = self._add(self.technician)
        self.assertFalse(part.is_consumable)
        self.assertEqual(part.stock_qty, 0)
        part = self._add(self.manager, code="IMP-2")
        self.assertTrue(part.is_consumable)
        self.assertEqual(part.stock_qty, 5)

    def test_the_addition_is_logged_on_the_case(self):
        part = self._add(self.manager)
        entry = ActivityLog.objects.get(case=self.case, text__contains="SVC-10")
        self.assertEqual(entry.author, self.manager)
        self.assertIn(part.code, entry.text)
        self.assertIn("consumable: yes", entry.text)


class PartAdvanceTests(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.case = self._make_case(build_service_workflow(), "new_report")

    def test_the_advance_is_half_the_price_rounded_half_up(self):
        self.assertEqual(part_advance_amount(Decimal("100")), Decimal("50.00"))
        self.assertEqual(part_advance_amount(Decimal("0.01")), Decimal("0.01"))
        self.assertEqual(part_advance_amount(Decimal("33.33")), Decimal("16.67"))

    def test_a_non_stock_part_records_and_links_the_advance(self):
        part = Part.objects.create(code="NS-1", name="Ordered", is_consumable=False)
        order, advance = order_part_with_advance(self.case, part, Decimal("200"))
        self.assertIsNotNone(advance)
        self.assertEqual(advance.amount, Decimal("100.00"))
        self.assertEqual(advance.percent, 50)
        self.assertEqual(advance.case, self.case)
        self.assertEqual(order.advance_payment, advance)

    def test_a_stocked_consumable_needs_no_advance(self):
        part = Part.objects.create(
            code="ST-1", name="Stocked", is_consumable=True, stock_qty=3
        )
        order, advance = order_part_with_advance(self.case, part, Decimal("200"))
        self.assertIsNone(advance)
        self.assertIsNone(order.advance_payment)
