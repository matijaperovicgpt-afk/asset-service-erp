"""Tests for the OPEN-02 parts catalogue page (Katalog dijelova).

The page maintains ``Part`` rows and the ``is_consumable`` flag which,
together with ``stock_qty``, drives ``parts.services.is_stock_part``
(SVC-10) and therefore the 50% advance rule (SVC-11/INV-05, SVC-14).

Mirrors the role rules of the other config surfaces: any staff member may
view, only MANAGER/DIRECTOR (``PRICING_ROLES``) may mutate.
"""

import re
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.cases.exceptions import GuardCheckFailed
from apps.cases.models import Case, CaseType, ReportChannel, Status
from apps.cases.services import transition_case
from apps.cases.tests import build_service_workflow
from apps.parts.models import Part, PartOrder
from apps.parts.services import is_stock_part
from apps.service.models import UsedPart, WorkOrder


def make_part(code="IMP-1", name="Impeller", is_consumable=False, stock_qty=0):
    return Part.objects.create(
        code=code, name=name, is_consumable=is_consumable, stock_qty=stock_qty
    )


def make_case():
    status, _created = Status.objects.get_or_create(
        case_type=CaseType.SERVICE,
        code="new_report",
        defaults={"name": "New report", "client_visible_name": "New report"},
    )
    return Case.objects.create(
        case_type=CaseType.SERVICE,
        report_channel=ReportChannel.PHONE,
        status=status,
    )


class PartsCatalogUsersMixin:
    """The accounts the role rules are stated over, plus payload helpers."""

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.frontdesk = User.objects.create_user(
            username="partsfront", password="demo12345",
            is_staff=True, role="FRONT_DESK",
        )
        self.technician = User.objects.create_user(
            username="partstech", password="demo12345",
            is_staff=True, role="TECHNICIAN",
        )
        self.manager = User.objects.create_user(
            username="partsmgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.director = User.objects.create_user(
            username="partsdir", password="demo12345",
            is_staff=True, role="DIRECTOR",
        )
        self.outsider = User.objects.create_user(
            username="partsclient", password="demo12345", is_staff=False,
        )
        self.page = reverse("backoffice:parts-catalog")

    def add_payload(self, code="NEW-1", name="New part",
                    is_consumable=False, stock_qty=0):
        data = {"new-code": code, "new-name": name,
                "new-stock_qty": str(stock_qty)}
        if is_consumable:
            data["new-is_consumable"] = "on"
        return data

    def edit_payload(self, part, code=None, name=None,
                     is_consumable=None, stock_qty=None):
        prefix = "part-%s" % part.pk
        code = part.code if code is None else code
        name = part.name if name is None else name
        stock_qty = part.stock_qty if stock_qty is None else stock_qty
        is_consumable = (
            part.is_consumable if is_consumable is None else is_consumable
        )
        data = {
            "%s-code" % prefix: code,
            "%s-name" % prefix: name,
            "%s-stock_qty" % prefix: str(stock_qty),
        }
        if is_consumable:
            data["%s-is_consumable" % prefix] = "on"
        return data


class PartsCatalogEmptyTests(PartsCatalogUsersMixin, TestCase):
    """OPEN-02: nothing is seeded - the consumables list is still pending
    from management, so the catalogue legitimately starts empty."""

    def test_no_parts_are_seeded(self):
        self.assertEqual(Part.objects.count(), 0)

    def test_page_renders_the_empty_state(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No parts in the catalogue yet.")
        self.assertEqual(list(response.context["part_rows"]), [])


class PartsCatalogAccessTests(PartsCatalogUsersMixin, TestCase):
    """Any staff may view; mutations are MANAGER/DIRECTOR only."""

    def setUp(self):
        super().setUp()
        self.part = make_part()

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_authenticated_non_staff_is_forbidden(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.page).status_code, 403)

    def test_technician_may_view_but_gets_no_edit_controls(self):
        self.client.force_login(self.technician)
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IMP-1")
        self.assertFalse(response.context["can_edit"])
        self.assertNotContains(response, "part-form-new")
        self.assertContains(
            response, "Only a manager or director may edit the parts catalogue."
        )

    def test_front_desk_may_view_but_may_not_edit(self):
        self.client.force_login(self.frontdesk)
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_edit"])

    def test_manager_sees_edit_controls(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.page)
        self.assertTrue(response.context["can_edit"])
        self.assertContains(response, "part-form-new")

    def test_director_may_edit(self):
        self.client.force_login(self.director)
        self.assertTrue(self.client.get(self.page).context["can_edit"])

    def test_superuser_without_a_role_may_edit(self):
        User = get_user_model()
        root = User.objects.create_superuser(
            username="partsroot", password="demo12345", email=""
        )
        self.client.force_login(root)
        self.assertTrue(self.client.get(self.page).context["can_edit"])
        self.client.post(
            reverse("backoffice:part-add"), self.add_payload(code="ROOT-1")
        )
        self.assertTrue(Part.objects.filter(code="ROOT-1").exists())

    # --- direct POSTs as a TECHNICIAN are refused ---------------------
    def test_technician_add_is_refused(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("backoffice:part-add"),
            self.add_payload(code="TECH-1"),
            follow=True,
        )
        self.assertContains(response, "may not edit the parts catalogue")
        self.assertEqual(Part.objects.count(), 1)
        self.assertFalse(Part.objects.filter(code="TECH-1").exists())

    def test_technician_edit_is_refused(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, is_consumable=True, stock_qty=9),
            follow=True,
        )
        self.assertContains(response, "may not edit the parts catalogue")
        self.part.refresh_from_db()
        self.assertFalse(self.part.is_consumable)
        self.assertEqual(self.part.stock_qty, 0)

    def test_technician_delete_is_refused(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("backoffice:part-delete", args=[self.part.pk]), follow=True
        )
        self.assertContains(response, "may not edit the parts catalogue")
        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_front_desk_add_is_refused(self):
        self.client.force_login(self.frontdesk)
        self.client.post(
            reverse("backoffice:part-add"), self.add_payload(code="FD-1")
        )
        self.assertFalse(Part.objects.filter(code="FD-1").exists())

    def test_non_staff_post_is_forbidden(self):
        self.client.force_login(self.outsider)
        response = self.client.post(
            reverse("backoffice:part-add"), self.add_payload(code="OUT-1")
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Part.objects.filter(code="OUT-1").exists())


class PartsCatalogMutationTests(PartsCatalogUsersMixin, TestCase):
    """Create / edit / toggle as a manager."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)

    def test_manager_creates_a_part(self):
        response = self.client.post(
            reverse("backoffice:part-add"),
            self.add_payload(code="FLT-7", name="Fuel filter", stock_qty=3),
            follow=True,
        )
        self.assertContains(response, "added")
        part = Part.objects.get(code="FLT-7")
        self.assertEqual(part.name, "Fuel filter")
        self.assertEqual(part.stock_qty, 3)
        # Nothing becomes a consumable unless explicitly ticked (OPEN-02).
        self.assertFalse(part.is_consumable)
        self.assertFalse(is_stock_part(part))

    def test_created_consumable_with_stock_counts_as_stock_part(self):
        self.client.post(
            reverse("backoffice:part-add"),
            self.add_payload(
                code="OIL-1", name="Oil", is_consumable=True, stock_qty=10
            ),
            follow=True,
        )
        self.assertTrue(is_stock_part(Part.objects.get(code="OIL-1")))

    def test_duplicate_code_is_a_form_error_not_a_crash(self):
        make_part(code="DUP-1")
        response = self.client.post(
            reverse("backoffice:part-add"),
            self.add_payload(code="DUP-1", name="Other"),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Part.objects.filter(code="DUP-1").count(), 1)

    def test_negative_stock_is_refused(self):
        response = self.client.post(
            reverse("backoffice:part-add"),
            self.add_payload(code="NEG-1", stock_qty=-3),
            follow=True,
        )
        self.assertContains(response, "cannot be negative")
        self.assertFalse(Part.objects.filter(code="NEG-1").exists())

    def test_blank_code_is_refused(self):
        response = self.client.post(
            reverse("backoffice:part-add"),
            self.add_payload(code="", name="Nameless"),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Part.objects.count(), 0)

    def test_manager_edits_stock_qty(self):
        part = make_part(code="ANO-1", is_consumable=True, stock_qty=0)
        response = self.client.post(
            reverse("backoffice:part-edit", args=[part.pk]),
            self.edit_payload(part, stock_qty=4),
            follow=True,
        )
        self.assertContains(response, "updated")
        part.refresh_from_db()
        self.assertEqual(part.stock_qty, 4)

    def test_manager_edits_name_and_code(self):
        part = make_part(code="OLD-1", name="Old name")
        self.client.post(
            reverse("backoffice:part-edit", args=[part.pk]),
            self.edit_payload(part, code="NEW-9", name="New name"),
            follow=True,
        )
        part.refresh_from_db()
        self.assertEqual(part.code, "NEW-9")
        self.assertEqual(part.name, "New name")

    def test_editing_a_missing_part_is_404(self):
        response = self.client.post(
            reverse("backoffice:part-edit", args=[999999]), {}
        )
        self.assertEqual(response.status_code, 404)

    # --- toggling the consumable flag ---------------------------------
    def test_toggling_consumable_on_makes_it_a_stock_part(self):
        part = make_part(code="TOG-1", is_consumable=False, stock_qty=2)
        self.assertFalse(is_stock_part(part))
        response = self.client.post(
            reverse("backoffice:part-edit", args=[part.pk]),
            self.edit_payload(part, is_consumable=True),
            follow=True,
        )
        part.refresh_from_db()
        self.assertTrue(part.is_consumable)
        self.assertTrue(is_stock_part(part))
        self.assertContains(response, "now counts as a stocked consumable")

    def test_toggling_consumable_off_warns_about_open_orders(self):
        part = make_part(code="TOG-2", is_consumable=True, stock_qty=2)
        case = make_case()
        PartOrder.objects.create(case=case, part=part)
        response = self.client.post(
            reverse("backoffice:part-edit", args=[part.pk]),
            self.edit_payload(part, is_consumable=False),
            follow=True,
        )
        part.refresh_from_db()
        self.assertFalse(part.is_consumable)
        self.assertFalse(is_stock_part(part))
        self.assertContains(response, "no longer counts as a stocked")
        self.assertContains(response, "SVC-11/INV-05")

    def test_zeroing_the_stock_also_drops_the_stock_verdict(self):
        part = make_part(code="TOG-4", is_consumable=True, stock_qty=2)
        response = self.client.post(
            reverse("backoffice:part-edit", args=[part.pk]),
            self.edit_payload(part, stock_qty=0),
            follow=True,
        )
        part.refresh_from_db()
        self.assertTrue(part.is_consumable)
        self.assertFalse(is_stock_part(part))
        self.assertContains(response, "no longer counts as a stocked")

    def test_unchanged_stock_status_produces_no_warning(self):
        part = make_part(code="TOG-3", is_consumable=True, stock_qty=2)
        response = self.client.post(
            reverse("backoffice:part-edit", args=[part.pk]),
            self.edit_payload(part, name="Renamed"),
            follow=True,
        )
        self.assertNotContains(response, "no longer counts as a stocked")
        self.assertNotContains(response, "now counts as a stocked consumable")


class IsStockPartSemanticsTests(TestCase):
    """SVC-10: is_stock_part() == is_consumable AND stock_qty > 0."""

    def test_all_four_combinations(self):
        combos = [
            (True, 5, True),
            (True, 0, False),
            (False, 5, False),
            (False, 0, False),
        ]
        for index, (consumable, qty, expected) in enumerate(combos):
            with self.subTest(is_consumable=consumable, stock_qty=qty):
                part = make_part(
                    code="COMB-%s" % index,
                    is_consumable=consumable,
                    stock_qty=qty,
                )
                self.assertIs(is_stock_part(part), expected)

    def test_negative_stock_is_not_in_stock(self):
        part = make_part(code="NEGS-1", is_consumable=True, stock_qty=-1)
        self.assertFalse(is_stock_part(part))

    def test_flipping_the_flag_flips_is_stock_part(self):
        part = make_part(code="FLIP-1", is_consumable=True, stock_qty=3)
        self.assertTrue(is_stock_part(part))
        part.is_consumable = False
        self.assertFalse(is_stock_part(part))
        part.is_consumable = True
        self.assertTrue(is_stock_part(part))

    def test_page_reports_the_same_verdict_as_the_service(self):
        User = get_user_model()
        manager = User.objects.create_user(
            username="semmgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        make_part(code="A-1", is_consumable=True, stock_qty=1)
        make_part(code="B-1", is_consumable=True, stock_qty=0)
        make_part(code="C-1", is_consumable=False, stock_qty=7)
        self.client.force_login(manager)
        rows = self.client.get(
            reverse("backoffice:parts-catalog")
        ).context["part_rows"]
        verdicts = {row["part"].code: row["is_stock"] for row in rows}
        self.assertEqual(verdicts, {"A-1": True, "B-1": False, "C-1": False})


class PartDeleteTests(PartsCatalogUsersMixin, TestCase):
    """INV-03: deleting a catalogue row must never destroy case history."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)
        self.case = make_case()

    def test_unreferenced_part_is_deleted(self):
        part = make_part(code="FREE-1")
        response = self.client.post(
            reverse("backoffice:part-delete", args=[part.pk]), follow=True
        )
        self.assertContains(response, "deleted")
        self.assertFalse(Part.objects.filter(pk=part.pk).exists())

    def test_part_referenced_by_a_part_order_is_not_deleted(self):
        part = make_part(code="HIST-1", is_consumable=True, stock_qty=1)
        order = PartOrder.objects.create(case=self.case, part=part)
        response = self.client.post(
            reverse("backoffice:part-delete", args=[part.pk]), follow=True
        )
        self.assertContains(response, "cannot be deleted")
        # The catalogue row survives...
        self.assertTrue(Part.objects.filter(pk=part.pk).exists())
        # ...and so does the history that points at it (INV-03).
        self.assertEqual(PartOrder.objects.filter(pk=order.pk).count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.part_id, part.pk)

    def test_part_referenced_by_a_received_order_is_not_deleted(self):
        part = make_part(code="HIST-2")
        PartOrder.objects.create(
            case=self.case, part=part, received_at=timezone.now()
        )
        self.client.post(
            reverse("backoffice:part-delete", args=[part.pk]), follow=True
        )
        self.assertTrue(Part.objects.filter(pk=part.pk).exists())
        self.assertEqual(PartOrder.objects.count(), 1)

    def test_part_installed_on_a_work_order_is_not_deleted(self):
        part = make_part(code="HIST-3")
        work_order = WorkOrder.objects.create(case=self.case)
        used = UsedPart.objects.create(
            work_order=work_order, part=part, quantity=2
        )
        response = self.client.post(
            reverse("backoffice:part-delete", args=[part.pk]), follow=True
        )
        self.assertContains(response, "cannot be deleted")
        self.assertTrue(Part.objects.filter(pk=part.pk).exists())
        self.assertTrue(UsedPart.objects.filter(pk=used.pk).exists())

    def test_refusal_message_names_the_way_out(self):
        part = make_part(code="HIST-4")
        PartOrder.objects.create(case=self.case, part=part)
        response = self.client.post(
            reverse("backoffice:part-delete", args=[part.pk]), follow=True
        )
        self.assertContains(response, "INV-03")
        # It must NOT prescribe clearing the flag as a retirement: that is
        # not one (the part stays orderable) and it flips SVC-10 for the
        # open orders that already point at the part.
        self.assertContains(response, "no way to retire a part yet")
        self.assertNotContains(response, "Retire it instead")

    def test_catalogue_marks_referenced_rows_as_undeletable(self):
        part = make_part(code="HIST-5")
        PartOrder.objects.create(case=self.case, part=part)
        response = self.client.get(self.page)
        row = response.context["part_rows"][0]
        self.assertTrue(row["is_referenced"])
        self.assertEqual(row["order_count"], 1)
        self.assertEqual(row["open_order_count"], 1)
        self.assertEqual(row["used_count"], 0)
        # Said once, in a footnote under the table, and pointed at from the
        # disabled button - not repeated inside every referenced row.
        self.assertContains(response, "Referenced by case history", count=1)
        self.assertContains(response, 'id="parts-referenced-note"')
        self.assertContains(response, 'aria-describedby="parts-referenced-note"')

    def test_footnote_is_absent_when_nothing_is_referenced(self):
        make_part(code="FREE-1")
        response = self.client.get(self.page)
        self.assertNotContains(response, "parts-referenced-note")
        self.assertNotContains(response, "Referenced by case history")

    def test_counts_are_not_multiplied_by_the_two_reverse_joins(self):
        part = make_part(code="HIST-6")
        PartOrder.objects.create(case=self.case, part=part)
        PartOrder.objects.create(case=self.case, part=part)
        work_order = WorkOrder.objects.create(case=self.case)
        UsedPart.objects.create(work_order=work_order, part=part)
        response = self.client.get(self.page)
        row = response.context["part_rows"][0]
        self.assertEqual(row["order_count"], 2)
        self.assertEqual(row["used_count"], 1)

    def test_open_and_total_order_counts_are_reported_separately(self):
        part = make_part(code="HIST-7")
        PartOrder.objects.create(case=self.case, part=part)
        PartOrder.objects.create(
            case=self.case, part=part, received_at=timezone.now()
        )
        work_order = WorkOrder.objects.create(case=self.case)
        UsedPart.objects.create(work_order=work_order, part=part)
        UsedPart.objects.create(work_order=work_order, part=part, quantity=3)
        row = self.client.get(self.page).context["part_rows"][0]
        self.assertEqual(row["order_count"], 2)
        self.assertEqual(row["open_order_count"], 1)
        self.assertEqual(row["used_count"], 2)

    def test_deleting_a_missing_part_is_404(self):
        response = self.client.post(
            reverse("backoffice:part-delete", args=[999999])
        )
        self.assertEqual(response.status_code, 404)


class ConsumableFlagDrivesTheAwaitingPartGuardTests(
    PartsCatalogUsersMixin, TestCase
):
    """The point of the flag: editing it in the catalogue changes what
    SVC-14 allows, because the guard re-evaluates is_stock_part() at
    transition time (the warning the edit view shows says exactly that).
    """

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Test Contact",
            status=self.statuses["awaiting_approval"],
        )
        self.part = make_part(code="SVC10-1", is_consumable=True, stock_qty=3)
        PartOrder.objects.create(
            case=self.case,
            part=self.part,
            expected_delivery=timezone.localdate() + timedelta(days=5),
        )
        self.client.force_login(self.manager)

    def _to_awaiting_part(self):
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                return transition_case(self.case, "awaiting_part", self.manager)

    def test_stock_part_needs_no_advance(self):
        self.assertEqual(self._to_awaiting_part().status.code, "awaiting_part")

    def test_unticking_consumable_in_the_catalogue_blocks_the_transition(self):
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, is_consumable=False),
            follow=True,
        )
        self.part.refresh_from_db()
        self.assertFalse(is_stock_part(self.part))
        with self.assertRaisesMessage(GuardCheckFailed, "non-stock"):
            self._to_awaiting_part()

    def test_zeroing_the_stock_in_the_catalogue_blocks_the_transition(self):
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=0),
            follow=True,
        )
        with self.assertRaisesMessage(GuardCheckFailed, "non-stock"):
            self._to_awaiting_part()


class StockAuditTrailTests(PartsCatalogUsersMixin, TestCase):
    """A hand-edited stock quantity leaves a trace.

    ``receive_part_order`` logs every unit it adds; this form could set the
    same number to anything and logged nothing, so the one place stock
    moved for a documented reason was recorded and the one place it moved
    arbitrarily was not. ``ActivityLog`` is case-scoped, so the line goes
    on the cases the change actually affects: those holding an open order
    for the part, whose SVC-10/SVC-11 treatment this number decides.
    """

    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)
        self.part = make_part(code="IMP-1", is_consumable=True, stock_qty=2)

    def stock_logs(self, case=None):
        from apps.cases.models import ActivityLog

        entries = ActivityLog.objects.filter(
            kind=ActivityLog.Kind.SYSTEM, text__contains="Stock quantity"
        )
        return entries.filter(case=case) if case is not None else entries

    def test_editing_the_stock_logs_on_each_case_with_an_open_order(self):
        first, second = make_case(), make_case()
        PartOrder.objects.create(case=first, part=self.part)
        PartOrder.objects.create(case=second, part=self.part)
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=9),
            follow=True,
        )
        self.assertEqual(self.stock_logs().count(), 2)
        entry = self.stock_logs(first).get()
        self.assertIn("IMP-1", entry.text)
        self.assertIn("from 2 to 9", entry.text)
        self.assertEqual(entry.author, self.manager)

    def test_a_received_order_is_not_an_open_one(self):
        case = make_case()
        PartOrder.objects.create(
            case=case, part=self.part, received_at=timezone.now()
        )
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=9),
            follow=True,
        )
        self.assertEqual(self.stock_logs().count(), 0)

    def test_two_open_orders_on_one_case_log_once(self):
        case = make_case()
        PartOrder.objects.create(case=case, part=self.part)
        PartOrder.objects.create(case=case, part=self.part)
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=9),
            follow=True,
        )
        self.assertEqual(self.stock_logs(case).count(), 1)

    def test_a_case_whose_diagnostic_names_the_part_is_told_too(self):
        """SVC-10 reads the same number when the diagnostic claims the
        store holds enough - so that case has to hear about the change
        even with no order raised on it."""
        from apps.service.models import Diagnostic, DiagnosticPart

        case = make_case()
        work_order = WorkOrder.objects.create(case=case)
        diagnostic = Diagnostic.objects.create(
            work_order=work_order,
            issue="Ne pali",
            servicing_work="Zamena impelera",
            estimated_parts_price=Decimal("40"),
            estimated_completion_date=timezone.localdate(),
        )
        DiagnosticPart.objects.create(
            diagnostic=diagnostic, part=self.part, quantity=2
        )
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=1),
            follow=True,
        )
        self.assertIn("from 2 to 1", self.stock_logs(case).get().text)

    def test_a_part_nothing_points_at_yet_has_nowhere_to_be_logged(self):
        # The known limit of a case-scoped log, stated so the next reader
        # does not mistake it for an oversight.
        make_case()
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=9),
            follow=True,
        )
        self.part.refresh_from_db()
        self.assertEqual(self.part.stock_qty, 9)
        self.assertEqual(self.stock_logs().count(), 0)

    def test_editing_something_else_logs_nothing(self):
        case = make_case()
        PartOrder.objects.create(case=case, part=self.part)
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, name="Impeller (Alpha I)"),
            follow=True,
        )
        self.part.refresh_from_db()
        self.assertEqual(self.part.name, "Impeller (Alpha I)")
        self.assertEqual(self.stock_logs().count(), 0)

    def test_the_entry_records_the_resulting_stock_verdict(self):
        case = make_case()
        PartOrder.objects.create(case=case, part=self.part)
        self.client.post(
            reverse("backoffice:part-edit", args=[self.part.pk]),
            self.edit_payload(self.part, stock_qty=0),
            follow=True,
        )
        entry = self.stock_logs(case).get()
        self.assertIn("from 2 to 0", entry.text)
        self.assertIn("SVC-10", entry.text)
        self.assertIn("no", entry.text)
        self.assertFalse(is_stock_part(Part.objects.get(pk=self.part.pk)))


class PartsCatalogEmptyRowTests(PartsCatalogUsersMixin, TestCase):
    """The empty-state row spans the table it sits in - which matters
    because the catalogue legitimately starts empty, so this is what the
    page shows on its first day."""

    def head_and_span(self):
        response = self.client.get(self.page)
        html = response.content.decode()
        head = html.split("</thead>")[0]
        columns = len(re.findall(r"<th[ >]", head))
        span = re.search(r'<td colspan="(\d+)"[^>]*>\s*No parts', html)
        return columns, int(span.group(1))

    def test_manager_sees_a_row_spanning_every_column(self):
        self.client.force_login(self.manager)
        columns, span = self.head_and_span()
        self.assertEqual(span, columns)

    def test_a_viewer_without_the_actions_column_sees_the_same(self):
        self.client.force_login(self.technician)
        columns, span = self.head_and_span()
        self.assertEqual(span, columns)
