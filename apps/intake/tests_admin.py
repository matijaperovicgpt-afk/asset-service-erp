"""Admin whitelist regressions from the 2026-11-05 review of phase D."""

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.cases.models import CaseType
from apps.cases.tests import WorkflowTestBase, build_workflow

from .admin import HandoverRecordAdmin, IntakeRecordAdmin
from .models import HandoverRecord, IntakeRecord


class SignedRecordsStayOnTheirCaseTests(WorkflowTestBase):
    """A signed intake record proves TRP-06 / asset-received for *its* case;
    the admin must not be able to move it to another one."""

    def setUp(self):
        super().setUp()
        statuses = build_workflow(CaseType.TRANSPORT, ["new_report", "noted"])
        self.case = self._make_case(statuses, "new_report")
        self.worker = get_user_model().objects.create_user(
            username="worker", password="x", role="TECHNICIAN"
        )

    def test_a_signed_intake_record_keeps_its_case(self):
        record = IntakeRecord.objects.create(
            case=self.case, received_by=self.worker, signed_at=timezone.now()
        )
        readonly = IntakeRecordAdmin(IntakeRecord, AdminSite()).get_readonly_fields(
            None, record
        )
        self.assertIn("case", readonly)
        self.assertIn("received_by", readonly)

    def test_an_unsigned_intake_record_may_still_be_moved(self):
        record = IntakeRecord.objects.create(case=self.case, received_by=self.worker)
        readonly = IntakeRecordAdmin(IntakeRecord, AdminSite()).get_readonly_fields(
            None, record
        )
        self.assertNotIn("case", readonly)

    def test_a_worker_signed_handover_keeps_its_case(self):
        record = HandoverRecord.objects.create(
            case=self.case,
            signed_by_technician=self.worker,
            technician_signed_at=timezone.now(),
        )
        readonly = HandoverRecordAdmin(HandoverRecord, AdminSite()).get_readonly_fields(
            None, record
        )
        self.assertIn("case", readonly)
        self.assertIn("technician_signed_at", readonly)
        self.assertNotIn("received_by_name", readonly)

    def test_a_fully_signed_handover_is_entirely_read_only(self):
        record = HandoverRecord.objects.create(
            case=self.case,
            signed_by_technician=self.worker,
            technician_signed_at=timezone.now(),
            signed_at=timezone.now(),
        )
        readonly = HandoverRecordAdmin(HandoverRecord, AdminSite()).get_readonly_fields(
            None, record
        )
        self.assertIn("received_by_name", readonly)
        self.assertIn("case", readonly)
