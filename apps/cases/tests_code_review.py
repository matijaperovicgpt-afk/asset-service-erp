"""Regressions from the 2026-11-04 module code review (cases)."""

from .models import Case, CaseType
from .tests import WorkflowTestBase, build_workflow


class TransitionCaseSnapshotTests(WorkflowTestBase):
    """``transition_case`` judges and saves the row as it is NOW, not the
    caller's snapshot: a bare ``save()`` used to write every column back
    and undo another writer's concurrent change."""

    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.SERVICE, ["new_report", "noted"])
        self.case = self._make_case(self.statuses, "new_report")

    def test_a_concurrent_edit_survives_the_transition(self):
        other = Case.objects.get(pk=self.case.pk)
        other.location = "Depot A, gate C"
        other.save(update_fields=["location"])

        self._transition(self.case, "noted")

        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["noted"])
        self.assertEqual(self.case.location, "Depot A, gate C")

    def test_the_callers_instance_is_refreshed(self):
        other = Case.objects.get(pk=self.case.pk)
        other.location = "Elsewhere"
        other.save(update_fields=["location"])

        returned = self._transition(self.case, "noted")

        self.assertIs(returned, self.case)
        self.assertEqual(self.case.location, "Elsewhere")


class CaseAdminReadOnlyTests(WorkflowTestBase):
    """The admin may read what the services write under their own rules
    (settlement roles, SVC-19 log, SERVICE-only work location), not rewrite it."""

    def test_service_owned_fields_are_read_only_on_change(self):
        from django.contrib.admin.sites import AdminSite

        from .admin import CaseAdmin

        statuses = build_workflow(CaseType.SERVICE, ["new_report", "noted"])
        case = self._make_case(statuses, "new_report")
        readonly = CaseAdmin(Case, AdminSite()).get_readonly_fields(None, case)
        for name in ("status", "work_location", "delivery_method",
                     "financially_settled", "contact_role", "contact_phone"):
            self.assertIn(name, readonly)
        self.assertNotIn("status", CaseAdmin(Case, AdminSite()).get_readonly_fields(None, None))


class ActivityEntryTests(WorkflowTestBase):
    """Phase E: the inline log form's INV-15 rule moved into the service."""

    def test_an_open_case_takes_a_staff_entry(self):
        from .models import ActivityLog
        from .services import add_activity_entry

        statuses = build_workflow(CaseType.SERVICE, ["new_report", "noted"])
        case = self._make_case(statuses, "new_report")
        entry = add_activity_entry(
            case, self.user, ActivityLog.Kind.PHONE_AGREEMENT, "Agreed 300 EUR."
        )
        self.assertEqual(entry.author, self.user)
        self.assertEqual(entry.kind, ActivityLog.Kind.PHONE_AGREEMENT)

    def test_a_closed_case_refuses_the_entry(self):
        from django.core.exceptions import ValidationError

        from .models import ActivityLog
        from .services import add_activity_entry

        statuses = build_workflow(CaseType.SERVICE, ["new_report", "closed"])
        statuses["closed"].is_terminal = True
        statuses["closed"].save(update_fields=["is_terminal"])
        case = self._make_case(statuses, "closed")
        with self.assertRaisesMessage(ValidationError, "is closed"):
            add_activity_entry(case, self.user, ActivityLog.Kind.NOTE, "late")
        self.assertFalse(ActivityLog.objects.filter(case=case, text="late").exists())


class AssetAttachmentsQueryTests(WorkflowTestBase):
    """Phase E: the asset gallery's target walk is a fixed number of
    queries, not five per case."""

    def _asset_with_cases(self, statuses, count):
        from decimal import Decimal

        from apps.clients.models import Client, Asset

        client = Client.objects.create(name="Owner %s" % count, phone="+44 67 000 00%s" % count)
        asset = Asset.objects.create(client=client, name="V%s" % count, length_m=Decimal("8.0"))
        for _ in range(count):
            self._make_case(statuses, "new_report", client=client, asset=asset)
        return asset

    def test_query_count_does_not_grow_with_the_number_of_cases(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from .services import asset_attachments

        statuses = build_workflow(CaseType.SERVICE, ["new_report", "noted"])
        one = self._asset_with_cases(statuses, 1)
        five = self._asset_with_cases(statuses, 5)
        list(asset_attachments(one))  # warm the ContentType cache
        with CaptureQueriesContext(connection) as single:
            list(asset_attachments(one))
        with CaptureQueriesContext(connection) as many:
            list(asset_attachments(five))
        self.assertEqual(len(single), len(many))
