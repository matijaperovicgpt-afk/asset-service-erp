"""Tests for the service app.

Workflow guard tests (SVC-09/SVC-16 over diagnostics and test runs)
live in the cases app; the ``record_test_run`` service is tested here.
"""
import shutil
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings

from django.core.exceptions import ValidationError

from apps.cases.models import (
    ActivityLog,
    Case,
    CaseType,
    ReportChannel,
    Status,
    StatusTransition,
)
from apps.clients.tests import link_identified_asset

from .models import InspectionPoint, InspectionRecord, TestRun, WorkOrder
from .services import (
    attach_client_approval,
    create_work_order,
    finalize_work_order,
    has_client_approval,
    inspection_complete_for_case,
    is_new_client,
    record_inspection_result,
    record_test_run,
    sign_inspection_record,
    sign_work_order,
    start_inspection,
)


class InspectionChecklistTests(TestCase):
    """SVC-08: the new-client full inspection - open, record, sign, plus the
    new-client detection and completion helpers the transition guard uses."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="fd", password="demo12345", role="FRONT_DESK"
        )
        self.tech = User.objects.create_user(
            username="tech", password="demo12345", role="TECHNICIAN"
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="diagnostics",
            name="d",
            client_visible_name="d",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.work_order = WorkOrder.objects.create(case=self.case)
        self.p1 = InspectionPoint.objects.create(
            system="STRUCTURE", label="Frame/chassis", sort_order=0
        )
        self.p2 = InspectionPoint.objects.create(
            system="ENGINE", label="Motor", sort_order=1
        )
        # Inactive points must never be materialised into a checklist.
        InspectionPoint.objects.create(
            system="OTHER", label="Staro", sort_order=2, is_active=False
        )

    def _resolve_all(self, record, status="OK"):
        for item in record.items.all():
            record_inspection_result(item, self.tech, status=status)

    def test_start_materialises_active_points_and_logs(self):
        record = start_inspection(self.work_order, self.tech)
        self.assertEqual(record.performed_by, self.tech)
        self.assertEqual(record.items.count(), 2)  # inactive point skipped
        self.assertEqual(
            set(record.items.values_list("point_id", flat=True)),
            {self.p1.pk, self.p2.pk},
        )
        self.assertTrue(all(not i.status for i in record.items.all()))
        entry = ActivityLog.objects.filter(
            case=self.case, text__contains="SVC-08"
        ).get()
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)

    def test_start_refuses_duplicate(self):
        start_inspection(self.work_order, self.tech)
        with self.assertRaises(ValidationError):
            start_inspection(self.work_order, self.tech)
        self.assertEqual(InspectionRecord.objects.count(), 1)

    def test_record_result_sets_status_and_strips_note(self):
        record = start_inspection(self.work_order, self.tech)
        item = record.items.get(point=self.p1)
        record_inspection_result(item, self.tech, status="DEFECT", note="  crack  ")
        item.refresh_from_db()
        self.assertEqual(item.status, "DEFECT")
        self.assertEqual(item.note, "crack")

    def test_record_result_rejects_invalid_status(self):
        record = start_inspection(self.work_order, self.tech)
        with self.assertRaises(ValidationError):
            record_inspection_result(record.items.first(), self.tech, status="BOGUS")

    def test_record_result_refused_after_signing(self):
        record = start_inspection(self.work_order, self.tech)
        self._resolve_all(record)
        sign_inspection_record(record, self.tech)
        with self.assertRaisesMessage(ValidationError, "signed"):
            record_inspection_result(record.items.first(), self.tech, status="DEFECT")

    def test_sign_requires_all_points_resolved(self):
        record = start_inspection(self.work_order, self.tech)
        record_inspection_result(
            record.items.get(point=self.p1), self.tech, status="OK"
        )  # p2 left blank
        with self.assertRaisesMessage(ValidationError, "Resolve all"):
            sign_inspection_record(record, self.tech)
        record.refresh_from_db()
        self.assertIsNone(record.signed_at)

    def test_sign_requires_points_present(self):
        InspectionPoint.objects.update(is_active=False)
        record = start_inspection(self.work_order, self.tech)
        self.assertEqual(record.items.count(), 0)
        with self.assertRaisesMessage(ValidationError, "no checklist points"):
            sign_inspection_record(record, self.tech)

    def test_sign_attributes_to_responsible_technician_once(self):
        self.case.assigned_technician = self.tech
        self.case.save()
        record = start_inspection(self.work_order, self.user)
        self._resolve_all(record)
        sign_inspection_record(record, self.user)  # acting user = front desk...
        record.refresh_from_db()
        self.assertEqual(record.signed_by, self.tech)  # ...credited to the tech
        self.assertIsNotNone(record.signed_at)
        with self.assertRaisesMessage(ValidationError, "already signed"):
            sign_inspection_record(record, self.user)

    def test_sign_falls_back_to_acting_user_when_unassigned(self):
        record = start_inspection(self.work_order, self.user)
        self._resolve_all(record)
        sign_inspection_record(record, self.tech)
        record.refresh_from_db()
        self.assertEqual(record.signed_by, self.tech)

    def test_front_desk_cannot_become_the_signer(self):
        """QA R-L2: with nobody assigned, the front desk signed in its
        own name an inspection it never performed."""
        record = start_inspection(self.work_order, self.user)
        self._resolve_all(record)
        with self.assertRaisesMessage(ValidationError, "SVC-08"):
            sign_inspection_record(record, self.user)
        record.refresh_from_db()
        self.assertIsNone(record.signed_at)

    def test_inspection_complete_tracks_signature(self):
        self.assertFalse(inspection_complete_for_case(self.case))
        record = start_inspection(self.work_order, self.tech)
        self._resolve_all(record)
        self.assertFalse(inspection_complete_for_case(self.case))  # unsigned
        sign_inspection_record(record, self.tech)
        self.assertTrue(inspection_complete_for_case(self.case))

    def test_is_new_client_unlinked_then_first_time_then_returning(self):
        from apps.clients.models import Client

        self.assertTrue(is_new_client(self.case))  # unlinked = new

        client = Client.objects.create(name="Mark")
        self.case.client = client
        self.case.save()
        self.assertTrue(is_new_client(self.case))  # linked, no prior terminal case

        terminal = Status.objects.create(
            case_type=CaseType.SERVICE, code="closed", name="c",
            client_visible_name="c", is_terminal=True,
        )
        Case.objects.create(
            case_type=CaseType.SERVICE, report_channel=ReportChannel.PHONE,
            status=terminal, client=client,
        )
        self.assertFalse(is_new_client(self.case))  # prior closed case = returning

def make_scan(name="work-order.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 fake scan")


class ServiceSmokeTest(SimpleTestCase):
    def test_models_import(self):
        from . import models

        self.assertTrue(hasattr(models, "WorkOrder"))


class RecordTestRunTests(TestCase):
    """SVC-16: a FAIL result sends the case back to rework."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tech", password="demo12345")

        def make_status(code, sort_order):
            return Status.objects.create(
                case_type=CaseType.SERVICE,
                code=code,
                name=code,
                client_visible_name=code,
                sort_order=sort_order,
            )

        self.in_progress = make_status("in_progress", 0)
        self.quality_check = make_status("quality_check", 1)
        StatusTransition.objects.create(
            from_status=self.in_progress, to_status=self.quality_check
        )
        StatusTransition.objects.create(
            from_status=self.quality_check, to_status=self.in_progress
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.quality_check,
        )
        self.work_order = WorkOrder.objects.create(case=self.case)

    def _record(self, result, **kwargs):
        # SVC-16 (procedure §4.2) now asks which control was performed and
        # whether the reported fault is cleared; a PASS requires the latter.
        kwargs.setdefault("kind", TestRun.Kind.TEST_RUN)
        kwargs.setdefault("fault_cleared", result == TestRun.Result.PASSED)
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                return record_test_run(
                    self.work_order, result, self.user, **kwargs
                )

    def test_fail_sends_case_back_to_in_progress(self):
        test_run = self._record(TestRun.Result.FAIL)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.in_progress)
        history = self.case.status_history.get()
        self.assertIn(str(test_run.pk), history.note)

    def test_pass_leaves_status_unchanged(self):
        self._record(TestRun.Result.PASSED)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.quality_check)
        self.assertEqual(self.case.status_history.count(), 0)

    def test_fail_outside_quality_check_records_run_only(self):
        self.case.status = self.in_progress
        self.case.save()
        self._record(TestRun.Result.FAIL)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.in_progress)
        self.assertEqual(TestRun.objects.count(), 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class WorkOrderLifecycleTests(TestCase):
    """SVC-02/SVC-05: open, finalize, attach the client-approval scan and
    let the responsible technician sign the work order."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="fd", password="demo12345", role="FRONT_DESK"
        )
        self.tech = User.objects.create_user(
            username="tech", password="demo12345", role="TECHNICIAN"
        )
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

    def test_create_work_order_logs_and_refuses_duplicate(self):
        work_order = create_work_order(self.case, self.user)
        self.assertTrue(work_order.is_preliminary)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("preliminary", entry.text)
        with self.assertRaises(ValidationError):
            create_work_order(self.case, self.user)
        self.assertEqual(WorkOrder.objects.count(), 1)

    def test_finalize_flips_once(self):
        work_order = create_work_order(self.case, self.user)
        finalize_work_order(work_order, self.user)
        work_order.refresh_from_db()
        self.assertFalse(work_order.is_preliminary)
        with self.assertRaises(ValidationError):
            finalize_work_order(work_order, self.user)

    def test_attach_client_approval_logs_and_flags(self):
        work_order = create_work_order(self.case, self.user)
        self.assertFalse(has_client_approval(work_order))
        attach_client_approval(work_order, make_scan(), self.user)
        self.assertTrue(has_client_approval(work_order))
        entry = ActivityLog.objects.filter(
            case=self.case, text__contains="client-approval"
        ).get()
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)

    def test_sign_sets_technician_and_timestamp_once(self):
        work_order = create_work_order(self.case, self.user)
        attach_client_approval(work_order, make_scan(), self.user)
        sign_work_order(work_order, self.tech)
        work_order.refresh_from_db()
        self.assertEqual(work_order.signed_by_technician, self.tech)
        self.assertIsNotNone(work_order.signed_at)
        with self.assertRaises(ValidationError):
            sign_work_order(work_order, self.tech)

    def test_sign_requires_client_approval_scan(self):
        work_order = create_work_order(self.case, self.user)
        with self.assertRaisesMessage(ValidationError, "client-approval scan"):
            sign_work_order(work_order, self.tech)
        work_order.refresh_from_db()
        self.assertIsNone(work_order.signed_at)

    def test_sign_requires_technician_role(self):
        work_order = create_work_order(self.case, self.user)
        attach_client_approval(work_order, make_scan(), self.user)
        with self.assertRaisesMessage(ValidationError, "Only a technician"):
            sign_work_order(work_order, self.user)
        work_order.refresh_from_db()
        self.assertIsNone(work_order.signed_at)

    def test_sign_assigns_responsible_technician_when_unset(self):
        work_order = create_work_order(self.case, self.user)
        attach_client_approval(work_order, make_scan(), self.user)
        sign_work_order(work_order, self.tech)
        self.case.refresh_from_db()
        self.assertEqual(self.case.assigned_technician, self.tech)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AssetReceivedIntegrationTests(TestCase):
    """SVC-05 end-to-end: create -> finalize -> client-approval scan ->
    technician signature + signed intake record satisfies the
    asset_received guard."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from apps.cases.tests import build_service_workflow

        User = get_user_model()
        self.user = User.objects.create_user(
            username="fd", password="demo12345", is_staff=True, role="FRONT_DESK"
        )
        self.tech = User.objects.create_user(
            username="tech", password="demo12345", is_staff=True, role="TECHNICIAN"
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["intake_scheduled"],
        )
        # §7.2: an intake record is a receipt for a particular asset.
        link_identified_asset(self.case)

    def test_full_signature_path_unlocks_asset_received(self):
        from apps.cases.services import transition_case
        from apps.intake.services import (
            confirm_no_intake_items,
            create_intake_record,
            sign_intake_record,
        )

        work_order = create_work_order(self.case, self.user)
        finalize_work_order(work_order, self.user)
        attach_client_approval(work_order, make_scan(), self.user)
        sign_work_order(work_order, self.tech)
        record = create_intake_record(
            self.case, self.user, mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN", storage_location="Hall 1",
            damage_note="none", energy_level="HALF",  # §7.6
        )
        # SVC-07: signing requires an arrival photo on the case.
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file

        attach_file(
            self.case,
            SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )
        confirm_no_intake_items(record, self.user)  # SVC-06
        sign_intake_record(record, self.user)

        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                transition_case(self.case, "asset_received", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "asset_received")


class ServicePriceItemTests(TestCase):
    """Official service price list catalog (01.10.2025, ACT-02)."""

    def test_ordering_and_str(self):
        from decimal import Decimal

        from .models import ServicePriceItem

        labor = ServicePriceItem.objects.create(
            category="LABOR",
            name="Radni sat",
            price=Decimal("50"),
            unit="PER_HOUR",
        )
        second = ServicePriceItem.objects.create(
            category="CALLOUT",
            name="Call-out — South",
            price=Decimal("90"),
            sort_order=1,
        )
        first = ServicePriceItem.objects.create(
            category="CALLOUT",
            name="Call-out — North",
            price=Decimal("60"),
            sort_order=0,
        )
        # Ordered by category, then sort_order (Category.values order is
        # not alphabetical, DB ordering is - CALLOUT < LABOR either way).
        self.assertEqual(
            list(ServicePriceItem.objects.all()), [first, second, labor]
        )
        self.assertEqual(str(labor), "Radni sat: 50 EUR / h")
        self.assertEqual(str(first), "Call-out — North: 60 EUR")

    def test_name_unique_within_category(self):
        from decimal import Decimal

        from django.db import IntegrityError

        from .models import ServicePriceItem

        ServicePriceItem.objects.create(
            category="OPERATION", name="Zamena elise", price=Decimal("60")
        )
        # The same name in another category is fine...
        ServicePriceItem.objects.create(
            category="PRESERVATION", name="Zamena elise", price=Decimal("1")
        )
        # ...but a duplicate within the category is rejected.
        with self.assertRaises(IntegrityError):
            ServicePriceItem.objects.create(
                category="OPERATION", name="Zamena elise", price=Decimal("99")
            )


class PreliminaryWorkOrderRegistrationTests(TestCase):
    """SVC-02 (procedure §1.2): once the client is known, the report is
    registered AND a preliminary work order is opened."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="fd-svc02", password="demo12345", role="FRONT_DESK"
        )
        self.status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="svc02_status",
            name="n",
            client_visible_name="n",
        )

    def _case(self, case_type=CaseType.SERVICE):
        status = self.status
        if case_type != CaseType.SERVICE:
            status = Status.objects.create(
                case_type=case_type,
                code="svc02_%s" % case_type.lower(),
                name="n",
                client_visible_name="n",
            )
        return Case.objects.create(
            case_type=case_type,
            report_channel=ReportChannel.PHONE,
            status=status,
        )

    def test_opens_a_preliminary_order_and_logs_it(self):
        from apps.service.services import ensure_preliminary_work_order

        case = self._case()
        work_order = ensure_preliminary_work_order(case, self.user)
        self.assertIsNotNone(work_order)
        self.assertTrue(work_order.is_preliminary)
        self.assertIsNone(work_order.signed_at)
        self.assertTrue(
            ActivityLog.objects.filter(
                case=case, text__contains="SVC-02"
            ).exists()
        )

    def test_is_idempotent(self):
        """Triage can be revisited; a second confirmation must not create
        a second order (WorkOrder.case is one-to-one anyway)."""
        from apps.service.services import ensure_preliminary_work_order

        case = self._case()
        first = ensure_preliminary_work_order(case, self.user)
        second = ensure_preliminary_work_order(case, self.user)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(WorkOrder.objects.filter(case=case).count(), 1)

    def test_does_not_touch_an_order_already_finalized(self):
        from apps.service.services import (
            ensure_preliminary_work_order,
            finalize_work_order,
        )

        case = self._case()
        work_order = ensure_preliminary_work_order(case, self.user)
        finalize_work_order(work_order, self.user)
        again = ensure_preliminary_work_order(case, self.user)
        again.refresh_from_db()
        self.assertFalse(again.is_preliminary)

    def test_non_service_cases_get_nothing(self):
        """A work order means nothing on a transport or storage case."""
        from apps.service.services import ensure_preliminary_work_order

        for case_type in (CaseType.TRANSPORT, CaseType.STORAGE):
            with self.subTest(case_type=case_type):
                case = self._case(case_type)
                self.assertIsNone(
                    ensure_preliminary_work_order(case, self.user)
                )
                self.assertFalse(WorkOrder.objects.filter(case=case).exists())


class WorkOrderEndorsementTests(TestCase):
    """SVC-05 (procedure §2.1): "The employee checks the data, endorses
    the order" - the third act on the document, now with a record of its own."""

    def setUp(self):
        User = get_user_model()
        self.clerk = User.objects.create_user(
            username="clerk-svc05", password="demo12345", role="FRONT_DESK"
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="svc05_status",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=True
        )

    def test_finalizing_records_who_endorsed_the_order(self):
        from apps.service.services import finalize_work_order

        finalize_work_order(self.work_order, self.clerk)
        self.work_order.refresh_from_db()
        self.assertFalse(self.work_order.is_preliminary)
        self.assertEqual(self.work_order.verified_by, self.clerk)
        self.assertIsNotNone(self.work_order.verified_at)
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="endorsed by 'clerk-svc05'"
            ).exists()
        )

    def test_endorsement_needs_a_staff_user(self):
        from apps.service.services import finalize_work_order

        with self.assertRaisesMessage(ValidationError, "SVC-05"):
            finalize_work_order(self.work_order, None)
        self.work_order.refresh_from_db()
        self.assertTrue(self.work_order.is_preliminary)
