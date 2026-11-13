"""SVC-10: the parts a diagnosis needs, as references instead of prose.

Procedure §3.2 turns on whether the parts are "consumable and available" -
on hand. While that answer lived in a free-text field the
shortcut guard could not read it at all, and said so in its own docstring.

The split is by IDENTITY, not by availability: the selector offers the
whole catalogue and the free text means "the catalogue has no row for
this". A part the catalogue knows but does not stock is still that part,
and retyping it as prose because the shelf is empty would throw away an
identity the system already holds. Which route the case takes is then
computed from ``stock_qty``, not from which input box somebody typed in.
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.cases.models import Case, CaseType, ReportChannel, Status
from apps.parts.models import Part
from apps.parts.services import delete_part
from apps.service.models import Diagnostic, DiagnosticPart, WorkOrder
from apps.service.services import record_diagnostic


class NeededPartsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="tech-svc10", password="demo12345", is_staff=True
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="svc10_status",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )

    def _part(self, code="IMP-1", consumable=True, stock=5):
        return Part.objects.create(
            code=code, name="Impeller", is_consumable=consumable,
            stock_qty=stock,
        )

    def _record(self, **kwargs):
        kwargs.setdefault("issue", "Impeller worn")
        kwargs.setdefault("servicing_work", "Replace it")
        kwargs.setdefault("estimated_parts_price", Decimal("40"))
        kwargs.setdefault("estimated_completion_date", timezone.now().date())
        return record_diagnostic(self.work_order, self.user, **kwargs)

    def test_catalogue_parts_are_recorded_as_references(self):
        part = self._part()
        diagnostic = self._record(needed_parts=[(part, 3)])
        needed = DiagnosticPart.objects.get(diagnostic=diagnostic)
        self.assertEqual(needed.part, part)
        self.assertEqual(needed.quantity, 3)
        self.assertEqual(diagnostic.parts_needed, "")

    def test_the_free_text_half_still_works_alone(self):
        """The catalogue will never be complete; a part it has no row for
        must still be recordable."""
        diagnostic = self._record(parts_needed="Custom bracket, welded")
        self.assertEqual(diagnostic.needed_parts.count(), 0)
        self.assertIn("Custom bracket", diagnostic.parts_needed)

    def test_both_halves_together(self):
        part = self._part()
        diagnostic = self._record(
            needed_parts=[(part, 1)], parts_needed="Custom bracket"
        )
        self.assertEqual(diagnostic.needed_parts.count(), 1)
        self.assertTrue(diagnostic.parts_needed)

    def test_neither_half_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "SVC-10"):
            self._record()

    def test_a_zero_quantity_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "at least 1"):
            self._record(needed_parts=[(self._part(), 0)])

    def test_an_out_of_stock_part_is_still_recordable(self):
        """Availability is not identity. The guard decides the route; the
        record just says which part it is."""
        part = self._part(stock=0)
        diagnostic = self._record(needed_parts=[(part, 2)])
        self.assertEqual(diagnostic.needed_parts.get().part, part)

    def test_the_log_names_how_many_and_whether_any_were_described(self):
        from apps.cases.models import ActivityLog

        self._record(needed_parts=[(self._part(), 1)], parts_needed="Bracket")
        entry = ActivityLog.objects.filter(
            case=self.case, text__contains="SVC-09"
        ).latest("pk")
        self.assertIn("1 catalogue part", entry.text)
        self.assertIn("uncatalogued", entry.text)

    def test_a_part_cannot_be_listed_twice_on_one_diagnostic(self):
        from django.db.utils import IntegrityError

        part = self._part()
        diagnostic = self._record(needed_parts=[(part, 1)])
        with self.assertRaises(IntegrityError):
            DiagnosticPart.objects.create(
                diagnostic=diagnostic, part=part, quantity=2
            )


class DiagnosticCompletenessTests(TestCase):
    """The SVC-09 completeness predicate the diagnostics guard reads.

    Requiring the free text outright - which it did while that was the
    only place parts could go - would make a fully-specified diagnosis
    count as incomplete and lock every edge out of ``diagnostics``.
    """

    def setUp(self):
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="svc10_complete",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.work_order = WorkOrder.objects.create(case=self.case)

    def _diagnostic(self, parts_needed=""):
        return Diagnostic.objects.create(
            work_order=self.work_order,
            issue="x",
            servicing_work="y",
            parts_needed=parts_needed,
            estimated_parts_price=Decimal("10"),
            estimated_completion_date=timezone.now().date(),
        )

    def test_structured_parts_alone_count_as_complete(self):
        diagnostic = self._diagnostic()
        DiagnosticPart.objects.create(
            diagnostic=diagnostic,
            part=Part.objects.create(
                code="P-C", name="P", is_consumable=True, stock_qty=1
            ),
            quantity=1,
        )
        self.assertIn(
            diagnostic, Diagnostic.complete_for_case(self.case)
        )

    def test_free_text_alone_counts_as_complete(self):
        diagnostic = self._diagnostic(parts_needed="Bracket")
        self.assertIn(diagnostic, Diagnostic.complete_for_case(self.case))

    def test_neither_is_incomplete(self):
        self._diagnostic()
        self.assertEqual(Diagnostic.complete_for_case(self.case).count(), 0)

    def test_several_parts_do_not_duplicate_the_diagnostic(self):
        """Annotated, not joined: a join to a multi-row relation would
        return the same diagnostic once per part."""
        diagnostic = self._diagnostic()
        for index in range(3):
            DiagnosticPart.objects.create(
                diagnostic=diagnostic,
                part=Part.objects.create(
                    code="P-%s" % index, name="P", is_consumable=True,
                    stock_qty=1,
                ),
                quantity=1,
            )
        self.assertEqual(Diagnostic.complete_for_case(self.case).count(), 1)


class DeletePartTests(TestCase):
    """INV-03: a diagnostic naming a part is case history as well."""

    def test_a_part_named_by_a_diagnostic_cannot_be_deleted(self):
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="svc10_delete",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        work_order = WorkOrder.objects.create(case=case)
        diagnostic = Diagnostic.objects.create(
            work_order=work_order,
            issue="x",
            servicing_work="y",
            estimated_parts_price=Decimal("10"),
            estimated_completion_date=timezone.now().date(),
        )
        part = Part.objects.create(
            code="P-DEL", name="P", is_consumable=True, stock_qty=1
        )
        DiagnosticPart.objects.create(
            diagnostic=diagnostic, part=part, quantity=1
        )
        with self.assertRaisesMessage(ValidationError, "1 diagnostic(s)"):
            delete_part(part)

    def test_an_unreferenced_part_still_deletes(self):
        part = Part.objects.create(
            code="P-FREE", name="P", is_consumable=True, stock_qty=1
        )
        delete_part(part)
        self.assertFalse(Part.objects.filter(pk=part.pk).exists())
