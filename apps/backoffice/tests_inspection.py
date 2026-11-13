"""SVC-08: the new-client full inspection is recorded as ONE form.

It used to be one form per checklist point, each with its own Save - a
first-time client's inspection runs to dozens of points, so recording it
meant dozens of posts, each rebuilding the whole case page. These pin the
replacement: every row travels in one POST, untouched rows stay as they
were, a bad row rejects the whole post without half-applying it, and the
rejected form comes back bound to the row that caused it.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel, Status
from apps.service.models import InspectionPoint, WorkOrder
from apps.service.services import (
    record_inspection_results,
    sign_inspection_record,
    start_inspection,
)


class InspectionResultsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.tech = User.objects.create_user(
            username="tech", password="demo12345", role="TECHNICIAN",
            is_staff=True,
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE, code="diagnostics",
            name="d", client_visible_name="d",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE, report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.work_order = WorkOrder.objects.create(case=self.case)
        for i, (system, label) in enumerate(
            [("STRUCTURE", "Structure"), ("ENGINE", "Engine"), ("FUEL", "Fuel")]
        ):
            InspectionPoint.objects.create(system=system, label=label, sort_order=i)
        self.record = start_inspection(self.work_order, self.tech)
        self.items = list(self.record.items.order_by("pk"))
        self.url = reverse(
            "backoffice:inspection-results", args=[self.case.pk, self.record.pk]
        )
        self.client.force_login(self.tech)

    def _post(self, **overrides):
        data = {}
        for item in self.items:
            data[f"status_{item.pk}"] = item.status
            data[f"note_{item.pk}"] = item.note
        data.update(overrides)
        return self.client.post(self.url, data)

    # ---- service ---------------------------------------------------------

    def test_service_applies_only_changed_rows_in_one_log_line(self):
        a, b, c = self.items
        changed = record_inspection_results(
            self.record, self.tech,
            {a.pk: ("OK", ""), b.pk: ("DEFECT", "  crack "), c.pk: ("", "")},
        )
        self.assertEqual(changed, 2)
        for row in (a, b, c):
            row.refresh_from_db()
        self.assertEqual((a.status, b.status, b.note, c.status), ("OK", "DEFECT", "crack", ""))
        lines = ActivityLog.objects.filter(case=self.case, text__contains="recorded")
        self.assertEqual(lines.count(), 1)
        self.assertIn("2 inspection point(s)", lines.get().text)

    def test_service_is_atomic_and_refuses_foreign_rows(self):
        a, b, _c = self.items
        with self.assertRaises(ValidationError):
            record_inspection_results(
                self.record, self.tech,
                {a.pk: ("OK", ""), b.pk: ("BOGUS", "")},
            )
        a.refresh_from_db()
        self.assertEqual(a.status, "", "row 1 must not survive a failed row 2")
        with self.assertRaises(ValidationError):
            record_inspection_results(self.record, self.tech, {999999: ("OK", "")})

    def test_service_refuses_a_signed_record(self):
        record_inspection_results(
            self.record, self.tech, {i.pk: ("OK", "") for i in self.items}
        )
        sign_inspection_record(self.record, self.tech)
        with self.assertRaises(ValidationError):
            record_inspection_results(self.record, self.tech, {self.items[0].pk: ("NA", "")})

    # ---- view + template ---------------------------------------------------

    def test_page_renders_one_form_for_the_whole_checklist(self):
        r = self.client.get(reverse("backoffice:case-detail", args=[self.case.pk]))
        body = r.content.decode()
        self.assertEqual(body.count(self.url), 1)
        for item in self.items:
            self.assertIn(f'name="status_{item.pk}"', body)
            self.assertIn(f'name="note_{item.pk}"', body)
        # The old per-row endpoint is no longer rendered anywhere.
        self.assertNotIn("/inspection/item/", body)

    def test_post_saves_every_row_at_once(self):
        a, b, c = self.items
        r = self._post(**{
            f"status_{a.pk}": "OK",
            f"status_{b.pk}": "ATTENTION", f"note_{b.pk}": "check belt",
            f"status_{c.pk}": "NA",
        })
        self.assertRedirects(
            r, reverse("backoffice:case-detail", args=[self.case.pk]),
            fetch_redirect_response=False,
        )
        for row in (a, b, c):
            row.refresh_from_db()
        self.assertEqual([a.status, b.status, c.status], ["OK", "ATTENTION", "NA"])
        self.assertEqual(b.note, "check belt")
        # Nothing unresolved: the inspection can now be signed in one click.
        sign_inspection_record(self.record, self.tech)

    def test_invalid_row_comes_back_bound_on_that_row(self):
        a, b, _c = self.items
        r = self._post(**{f"status_{a.pk}": "OK", f"note_{b.pk}": "x" * 501})
        self.assertEqual(r.status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.status, "", "a rejected post must not half-apply")
        page = self.client.get(reverse("backoffice:case-detail", args=[self.case.pk]))
        body = page.content.decode()
        # The section reopens with the typed input and the error on row b.
        self.assertIn('id="acc-inspection" class="collapse show"', body)
        self.assertIn('value="' + "x" * 501, body)
        self.assertIn("is-invalid", body)
        self.assertIn(f'name="note_{b.pk}"', body)
