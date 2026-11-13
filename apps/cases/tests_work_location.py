"""SVC-03: work done where the asset lies, instead of at the yard.

The procedure lets the responsible person consult a technician, before
the asset arrives, on whether the job can be done on site, on the
drive leg, or in the service centre. The first two answers mean the asset
never comes in - so the intake phase's products (an intake record, a receipt
confirmation, a handover record compared against it) cannot honestly be
written, and the guards must stop asking for them.

Blank (not consulted) is the state every case created before the field
existed is in, and it must keep behaving exactly as it did.
"""
import shutil
import tempfile

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from apps.cases.exceptions import GuardCheckFailed
from apps.cases.models import ActivityLog, WorkLocation
from apps.cases.services import record_work_location_consult
from apps.cases.tests import WorkflowTestBase, build_service_workflow


class WorkLocationConsultTests(WorkflowTestBase):
    """The SVC-03 consult itself."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "new_report")

    def test_records_the_answer_and_logs_the_consult(self):
        record_work_location_consult(
            self.case,
            WorkLocation.ON_SITE,
            self.user,
            note="Owner keeps the bay until Friday.",
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.work_location, WorkLocation.ON_SITE)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.NOTE)
        self.assertEqual(entry.author, self.user)
        self.assertIn("SVC-03", entry.text)
        self.assertIn("Owner keeps the bay", entry.text)

    def test_names_the_consulted_technician_in_the_log(self):
        User = self.user.__class__
        technician = User.objects.create_user(
            username="tech-svc03", password="demo12345", is_staff=True
        )
        record_work_location_consult(
            self.case,
            WorkLocation.IN_FIELD,
            self.user,
            consulted=technician,
        )
        self.assertIn("tech-svc03", ActivityLog.objects.get(case=self.case).text)

    def test_refuses_an_unknown_answer(self):
        with self.assertRaises(ValidationError):
            record_work_location_consult(self.case, "ON_THE_MOON", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.work_location, "")
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_refuses_on_site_once_a_receipt_is_signed(self):
        """A signed intake record says the asset WAS taken in; the two
        statements cannot both stand."""
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        IntakeRecord.objects.create(
            case=self.case,
            received_by=self.user,
            mount_status="OFF_MOUNT",
            signed_by=self.user,
            signed_at=timezone.now(),
        )
        with self.assertRaisesMessage(ValidationError, "signed intake record"):
            record_work_location_consult(
                self.case, WorkLocation.ON_SITE, self.user
            )
        # ...but recording it as a service-centre job is unaffected.
        record_work_location_consult(
            self.case, WorkLocation.SERVICE_CENTER, self.user
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.work_location, WorkLocation.SERVICE_CENTER)

    def test_blank_and_service_centre_still_expect_the_asset(self):
        self.assertTrue(self.case.requires_asset_intake)
        self.assertFalse(self.case.works_on_site)
        self.case.work_location = WorkLocation.SERVICE_CENTER
        self.assertTrue(self.case.requires_asset_intake)
        for location in (WorkLocation.ON_SITE, WorkLocation.IN_FIELD):
            with self.subTest(location=location):
                self.case.work_location = location
                self.assertFalse(self.case.requires_asset_intake)
                self.assertTrue(self.case.works_on_site)


class OnSiteWorkflowGuardTests(WorkflowTestBase):
    """The three guards that used to presume the asset comes in."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.case = self._make_case(
            self.statuses, "new_report", work_location=WorkLocation.ON_SITE
        )
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, True)

    def _book(self, kind):
        Appointment = django_apps.get_model("scheduling", "Appointment")
        from apps.scheduling.workinghours import next_working_slot

        return Appointment.objects.create(
            case=self.case, kind=kind, scheduled_for=next_working_slot()
        )

    def _signed_work_order(self):
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        work_order = WorkOrder.objects.create(
            case=self.case,
            is_preliminary=False,
            signed_by_technician=self.user,
            signed_at=timezone.now(),
        )
        from apps.service.services import attach_client_approval

        attach_client_approval(
            work_order,
            SimpleUploadedFile("work-order.pdf", b"%PDF-1.4 scan"),
            self.user,
        )
        return work_order

    def _visit_photo(self):
        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file

        return attach_file(
            self.case,
            SimpleUploadedFile("fault.jpg", b"\xff\xd8\xff\xe0 jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )

    def _link_identified_asset(self):
        """§7.2: an on-site case still has to say which asset it is."""
        from apps.clients.tests import link_identified_asset

        return link_identified_asset(self.case)

    # --- SVC-04 -------------------------------------------------------
    def test_intake_scheduled_needs_an_on_site_visit(self):
        with self.assertRaisesMessage(GuardCheckFailed, "on-site visit"):
            self._transition(self.case, "intake_scheduled")
        self._book("ON_SITE_VISIT")
        case = self._transition(self.case, "intake_scheduled")
        self.assertEqual(case.status.code, "intake_scheduled")

    def test_an_intake_appointment_also_satisfies_an_on_site_case(self):
        """The gate asks that a time was agreed, not that it carries the
        newer label - a visit booked as INTAKE is still an agreed time."""
        self._book("INTAKE")
        case = self._transition(self.case, "intake_scheduled")
        self.assertEqual(case.status.code, "intake_scheduled")

    # --- SVC-05/SVC-07 ------------------------------------------------
    def test_asset_received_without_a_intake(self):
        self.case.status = self.statuses["intake_scheduled"]
        self.case.save()
        with override_settings(MEDIA_ROOT=self.media_root):
            self._signed_work_order()
            # The work order alone is not enough: the asset is still
            # photographed before anyone touches it.
            with self.assertRaisesMessage(GuardCheckFailed, "at least one photo"):
                self._transition(self.case, "asset_received")
            self._visit_photo()
            # §7.2: and the ticket still has to say which asset it is
            # about - the datum belongs to the ticket, and this case
            # writes no intake record to carry it instead.
            with self.assertRaisesMessage(GuardCheckFailed, "which asset this is"):
                self._transition(self.case, "asset_received")
            self._link_identified_asset()
            case = self._transition(self.case, "asset_received")
        self.assertEqual(case.status.code, "asset_received")
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        self.assertFalse(IntakeRecord.objects.filter(case=self.case).exists())

    def test_the_work_order_records_are_still_required_on_site(self):
        self.case.status = self.statuses["intake_scheduled"]
        self.case.save()
        with override_settings(MEDIA_ROOT=self.media_root):
            self._visit_photo()
            with self.assertRaisesMessage(GuardCheckFailed, "final work order"):
                self._transition(self.case, "asset_received")

    def test_a_yard_case_still_needs_the_intake(self):
        """Regression: nothing changes for a case that is not on-site."""
        self.case.work_location = WorkLocation.SERVICE_CENTER
        self.case.status = self.statuses["intake_scheduled"]
        self.case.save()
        with override_settings(MEDIA_ROOT=self.media_root):
            self._signed_work_order()
            self._visit_photo()
            with self.assertRaisesMessage(GuardCheckFailed, "signed intake record"):
                self._transition(self.case, "asset_received")

    # --- SVC-19 -------------------------------------------------------
    def test_closed_needs_no_handover_record_on_site(self):
        self.case.status = self.statuses["ready_for_handover"]
        self.case.financially_settled = True
        self.case.save()
        case = self._transition(self.case, "closed")
        self.assertEqual(case.status.code, "closed")

    def test_closed_still_needs_settlement_on_site(self):
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()
        with self.assertRaisesMessage(GuardCheckFailed, "financial settlement"):
            self._transition(self.case, "closed")

    def test_a_yard_case_still_needs_the_handover_record(self):
        """Regression: the handover requirement is untouched off-site."""
        self.case.work_location = ""
        self.case.status = self.statuses["ready_for_handover"]
        self.case.financially_settled = True
        self.case.save()
        with self.assertRaisesMessage(GuardCheckFailed, "handover record"):
            self._transition(self.case, "closed")
