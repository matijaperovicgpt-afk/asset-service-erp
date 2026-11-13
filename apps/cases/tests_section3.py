"""Procedure §3.2 and §3.5: the two rules the workflow only described.

* §3.2 - the direct route from diagnostics to work is for a stock
  consumable inside the service's own approval limit. The wizard said so;
  nothing checked it.
* §3.5 - "Any additional work or cost increase... must be confirmed with
  the client beforehand." Nothing compared what the work cost against
  what the client had approved, so a 300 EUR job could finish at 3000.

The cost side is read from data the business already maintains: the
LABOR row of the service price list, and each part's catalogue price. A
price that cannot be read is a refusal, never a zero - a part counted as
free would defeat the comparison it exists for.
"""
from decimal import Decimal

from django.apps import apps as django_apps

from apps.accounts.models import ApprovalLimit, Role
from apps.cases.exceptions import GuardCheckFailed
from apps.cases.tests import WorkflowTestBase, build_service_workflow
from apps.parts.models import Part
from apps.quotes.models import Approval, Quote
from apps.service.models import ServicePriceItem, WorkOrder


def _labour_rate(price=Decimal("50"), name="Radni sat"):
    # (category, name) is unique, so a second rate needs its own name -
    # which is exactly the situation the guard refuses to guess about.
    return ServicePriceItem.objects.create(
        category=ServicePriceItem.Category.LABOR,
        name=name,
        price=price,
        unit=ServicePriceItem.Unit.PER_HOUR,
    )


class StockShortcutGuardTests(WorkflowTestBase):
    """SVC-10: `diagnostics -> in_progress` without a quote."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "diagnostics")
        self.work_order = WorkOrder.objects.create(case=self.case)
        Diagnostic = django_apps.get_model("service", "Diagnostic")
        self.diagnostic = Diagnostic.objects.create(
            work_order=self.work_order,
            issue="Impeller worn",
            servicing_work="Replace impeller",
            # SVC-10: the parts are identified from the catalogue, so the
            # guard can read them. Free text left over is a separate
            # refusal, pinned in apps/cases/tests.py.
            parts_needed="",
            estimated_parts_price=Decimal("80"),
            estimated_completion_date="2027-01-10",
        )
        DiagnosticPart = django_apps.get_model("service", "DiagnosticPart")
        DiagnosticPart.objects.create(
            diagnostic=self.diagnostic,
            part=Part.objects.create(
                code="IMP-STOCK", name="Impeller", is_consumable=True,
                stock_qty=4,
            ),
            quantity=1,
        )
        ApprovalLimit.objects.create(
            role=Role.TECHNICIAN, max_amount=Decimal("200")
        )

    def test_within_the_service_limit_the_shortcut_works(self):
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")

    def test_above_the_service_limit_it_is_refused(self):
        self.diagnostic.estimated_parts_price = Decimal("900")
        self.diagnostic.save()
        with self.assertRaisesMessage(GuardCheckFailed, "above the limit"):
            self._transition(self.case, "in_progress")

    def test_a_bought_in_part_sends_the_case_down_the_quote_path(self):
        """An open order for something that is not a stock consumable
        means the part had to be bought - the §3.3 route, not this one."""
        PartOrder = django_apps.get_model("parts", "PartOrder")
        part = Part.objects.create(
            code="IMP-9", name="Impeller", is_consumable=False, stock_qty=0
        )
        PartOrder.objects.create(case=self.case, part=part)
        with self.assertRaisesMessage(GuardCheckFailed, "had to be ordered"):
            self._transition(self.case, "in_progress")

    def test_a_stock_consumable_order_does_not_block_it(self):
        PartOrder = django_apps.get_model("parts", "PartOrder")
        part = Part.objects.create(
            code="OIL-1", name="Oil filter", is_consumable=True, stock_qty=12
        )
        PartOrder.objects.create(case=self.case, part=part)
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")

    def test_the_quote_path_is_unaffected(self):
        """The guard applies only to the direct edge; arriving from
        awaiting_approval is a different rule (SVC-13)."""
        # A returning client, so the SVC-08 first-time inspection (which
        # gates the quote path, not this one) stays out of the way.
        Client = django_apps.get_model("clients", "Client")
        client = Client.objects.create(name="Returning Owner")
        self.case.client = client
        self.case.save()
        self._make_case(self.statuses, "closed", client=client)
        self.diagnostic.estimated_parts_price = Decimal("900")
        self.diagnostic.save()
        quote = Quote.objects.create(
            case=self.case,
            description="Impeller",
            amount=Decimal("900"),
            status=Quote.Status.ACCEPTED,
        )
        for level in (Approval.Level.CLIENT, Approval.Level.DIRECTOR):
            Approval.objects.create(
                quote=quote, level=level, decision=Approval.Decision.APPROVED
            )
        self._transition(self.case, "awaiting_approval")
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")


class ExtraWorkGuardTests(WorkflowTestBase):
    """SVC-14: work may only be declared finished inside what was approved."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "in_progress")
        # Endorsed, not preliminary: a case that is in_progress has an
        # order somebody checked and endorsed, and SVC-15 now refuses to
        # record work against one that nobody has (procedure §4.1).
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )
        _labour_rate()
        ApprovalLimit.objects.create(
            role=Role.TECHNICIAN, max_amount=Decimal("200")
        )

    def _quote(self, amount, *, approved=True):
        """An accepted quote. When ``approved``, it carries every level
        its amount requires - a quote missing one is not an authorisation
        (INV-04), which is what the ladder is for."""
        from apps.quotes.services import required_approval_levels

        quote = Quote.objects.create(
            case=self.case,
            description="Agreed works",
            amount=Decimal(amount),
            status=Quote.Status.ACCEPTED,
        )
        if approved:
            for level in required_approval_levels(quote.amount):
                Approval.objects.create(
                    quote=quote,
                    level=level,
                    decision=Approval.Decision.APPROVED,
                )
        return quote

    def _log_hours(self, hours):
        from apps.service.services import log_work

        return log_work(
            self.work_order, self.user, description="Work", hours=Decimal(hours)
        )

    def _use_part(self, price, quantity=1):
        from apps.service.services import record_used_part

        part = Part.objects.create(
            code="P-%s" % (price or "X"),
            name="Part",
            is_consumable=True,
            stock_qty=5,
            unit_price=None if price is None else Decimal(price),
        )
        return record_used_part(
            self.work_order, self.user, part=part, quantity=quantity
        )

    def test_work_inside_the_approved_amount_passes(self):
        self._quote("500")
        self._log_hours("4")  # 4 x 50 = 200
        self._use_part("100")  # 300 total
        case = self._transition(self.case, "quality_check")
        self.assertEqual(case.status.code, "quality_check")

    def test_going_over_the_approved_amount_is_refused(self):
        self._quote("300")
        self._log_hours("5")  # 250
        self._use_part("100")  # 350 > 300
        with self.assertRaisesMessage(GuardCheckFailed, "against 300"):
            self._transition(self.case, "quality_check")

    def test_a_newer_approved_quote_unblocks_it(self):
        """The remedy the procedure names: agree the extra work with the
        client first, which is a new quote."""
        self._quote("300")
        self._log_hours("5")
        self._use_part("100")
        with self.assertRaises(GuardCheckFailed):
            self._transition(self.case, "quality_check")
        self._quote("600")
        case = self._transition(self.case, "quality_check")
        self.assertEqual(case.status.code, "quality_check")

    def test_a_quote_missing_its_approvals_does_not_count(self):
        """An accepted quote nobody internally signed off is not an
        authorisation (INV-04)."""
        ApprovalLimit.objects.filter(role=Role.TECHNICIAN).update(
            max_amount=Decimal("10")
        )
        self._quote("500", approved=False)
        self._log_hours("4")  # 200 > the 10 EUR fallback limit
        with self.assertRaises(GuardCheckFailed):
            self._transition(self.case, "quality_check")

    def test_without_a_quote_the_service_limit_is_the_ceiling(self):
        """Cases that took the SVC-10 shortcut have no quote at all; the
        authority is the ladder's technician row."""
        self._log_hours("3")  # 150 <= 200
        case = self._transition(self.case, "quality_check")
        self.assertEqual(case.status.code, "quality_check")

    def test_without_a_quote_over_the_service_limit_is_refused(self):
        self._log_hours("5")  # 250 > 200
        with self.assertRaisesMessage(GuardCheckFailed, "against 200"):
            self._transition(self.case, "quality_check")

    def test_an_unpriced_part_is_a_refusal_not_a_zero(self):
        self._quote("500")
        self._use_part(None)
        with self.assertRaisesMessage(GuardCheckFailed, "no price"):
            self._transition(self.case, "quality_check")

    def test_a_missing_labour_rate_is_a_refusal(self):
        ServicePriceItem.objects.all().delete()
        self._quote("500")
        self._log_hours("1")
        with self.assertRaisesMessage(GuardCheckFailed, "labour rate"):
            self._transition(self.case, "quality_check")

    def test_two_labour_rates_are_a_refusal(self):
        """The system cannot know which one applies; a human decides."""
        _labour_rate(Decimal("70"), name="Radni sat - hitna intervencija")
        self._quote("500")
        self._log_hours("1")
        with self.assertRaisesMessage(GuardCheckFailed, "expected exactly one"):
            self._transition(self.case, "quality_check")

    def test_nothing_recorded_costs_nothing(self):
        """A case with no work log and no parts is not blocked by a rule
        about exceeding an amount."""
        self._quote("100")
        case = self._transition(self.case, "quality_check")
        self.assertEqual(case.status.code, "quality_check")

    def test_the_rework_edge_is_unaffected(self):
        """quality_check -> in_progress must never be gated on this, or a
        failed test would lock the case in place."""
        self._quote("300")
        self._log_hours("5")
        self._use_part("100")
        self.case.status = self.statuses["quality_check"]
        self.case.save()
        case = self._transition(self.case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")
