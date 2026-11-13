"""Tests for the intake app.

Heavy workflow tests (state-machine guards over intake/handover records)
live in the cases app; only smoke tests belong here.
"""

import tempfile

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from apps.attachments.models import Attachment
from apps.cases.exceptions import AppendOnlyViolation
from apps.cases.models import Case, CaseType, ReportChannel, Status

from apps.cases.models import ActivityLog
from apps.clients.tests import link_identified_asset

from .models import HandoverRecord, IntakeAmendment, IntakeItem, IntakeRecord
from .services import (
    confirm_no_intake_items,
    add_intake_item,
    append_intake_amendment,
    attach_handover_approval,
    compare_handover_with_intake,
    create_handover_record,
    create_intake_record,
    remove_intake_item,
    sign_handover,
    sign_handover_technician,
    sign_intake_record,
    update_handover_note,
)


def _attach_handover_photo(handover, user=None):
    """§7/SVC-07 helper: photograph THIS handover.

    Filed on the record, not the case: a case may hold several handovers
    and each is photographed for itself.
    """
    from apps.attachments.services import attach_file

    return attach_file(
        handover,
        SimpleUploadedFile("handover.jpg", b"fake-jpeg"),
        Attachment.Kind.PHOTO,
        Attachment.Stage.HANDOVER,
        user=user,
    )


def _make_handover_signable(handover, user):
    """SVC-18 helper: the departure photos, the worker in-app signature
    and the client-signed scan - the three preconditions
    ``sign_handover()`` checks before confirming receipt."""
    _attach_handover_photo(handover, user)
    sign_handover_technician(handover, user)
    attach_handover_approval(
        handover, SimpleUploadedFile("handover.pdf", b"%PDF-1.4 scan"), user
    )
    return handover


class IntakeSmokeTests(SimpleTestCase):
    def test_models_importable(self):
        self.assertTrue(IntakeRecord)
        self.assertTrue(IntakeItem)
        self.assertTrue(HandoverRecord)

    def test_related_names(self):
        # The cases state machine queries these related names; they must not change.
        self.assertEqual(
            IntakeRecord._meta.get_field("case").remote_field.related_name,
            "intake_records",
        )
        self.assertEqual(
            HandoverRecord._meta.get_field("case").remote_field.related_name,
            "handover_records",
        )


class IntakeAmendmentTests(TestCase):
    """SVC-07 / INV-12: later findings are appended, never edited over."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tech", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.record = IntakeRecord.objects.create(
            case=case, general_condition_note="Scratch on starboard bow."
        )

    def test_append_intake_amendment(self):
        amendment = append_intake_amendment(
            self.record, "Hidden gelcoat crack found after washing.", self.user
        )
        self.assertEqual(amendment.record, self.record)
        self.assertEqual(amendment.author, self.user)
        self.assertEqual(self.record.amendments.count(), 1)

    def test_amendments_are_append_only(self):
        amendment = append_intake_amendment(self.record, "First finding.", self.user)
        amendment.text = "tampered"
        with self.assertRaises(AppendOnlyViolation):
            amendment.save()
        with self.assertRaises(AppendOnlyViolation):
            amendment.delete()
        self.assertEqual(
            IntakeAmendment.objects.get().text, "First finding."
        )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HandoverComparisonTests(TestCase):
    """SVC-18: the intake checklist is surfaced next to the handover."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tech", password="demo12345")
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
        self.handover = HandoverRecord.objects.create(
            case=self.case, discrepancies_note="Fender missing."
        )

    def _attach_to_case(self, stage, count=1):
        content_type = ContentType.objects.get_for_model(self.case)
        for i in range(count):
            Attachment.objects.create(
                content_type=content_type,
                object_id=self.case.pk,
                kind=Attachment.Kind.PHOTO,
                stage=stage,
                file=SimpleUploadedFile(f"photo{i}.jpg", b"fake-jpeg-bytes"),
            )

    def test_comparison_without_intake_record(self):
        comparison = compare_handover_with_intake(self.handover)
        self.assertIsNone(comparison["intake_record"])
        self.assertEqual(comparison["items"], [])
        self.assertEqual(comparison["amendments"], [])
        self.assertEqual(comparison["intake_photo_count"], 0)
        self.assertEqual(comparison["handover_photo_count"], 0)
        self.assertEqual(comparison["discrepancies_note"], "Fender missing.")

    def test_comparison_uses_latest_record_with_items_and_amendments(self):
        IntakeRecord.objects.create(case=self.case)  # older, superseded
        record = IntakeRecord.objects.create(case=self.case)
        IntakeItem.objects.create(
            record=record, item_name="Anchor", quantity=1, condition_note="rusty"
        )
        IntakeItem.objects.create(record=record, item_name="Life vest", quantity=4)
        append_intake_amendment(record, "Crack found after washing.", self.user)

        comparison = compare_handover_with_intake(self.handover)
        self.assertEqual(comparison["intake_record"], record)
        self.assertEqual(
            comparison["items"],
            [
                {
                    "category": "Other",
                    "item_name": "Anchor",
                    "quantity": 1,
                    "condition_note": "rusty",
                },
                {
                    "category": "Other",
                    "item_name": "Life vest",
                    "quantity": 4,
                    "condition_note": "",
                },
            ],
        )
        self.assertEqual(comparison["amendments"], ["Crack found after washing."])

    def test_comparison_prefers_the_signed_record_over_a_newer_draft(self):
        """QA SV-M3: an empty unsigned duplicate made after the signed
        receipt displaced it from the comparison."""
        signed = IntakeRecord.objects.create(
            case=self.case, signed_at=timezone.now()
        )
        IntakeRecord.objects.create(case=self.case)  # newer, unsigned
        comparison = compare_handover_with_intake(self.handover)
        self.assertEqual(comparison["intake_record"], signed)

    def test_photo_counts_by_stage(self):
        self._attach_to_case(Attachment.Stage.INTAKE, count=2)
        # §7/SVC-07: the departure photos belong to the record, so the
        # comparison counts THIS handover's rather than every handover's.
        _attach_handover_photo(self.handover)
        self._attach_to_case(Attachment.Stage.WORK, count=1)  # not counted

        comparison = compare_handover_with_intake(self.handover)
        self.assertEqual(comparison["intake_photo_count"], 2)
        self.assertEqual(comparison["handover_photo_count"], 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SignHandoverTests(TestCase):
    """SVC-18: receipt confirmed as name + timestamp, once only."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tech", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.handover = HandoverRecord.objects.create(case=case)

    def test_signing_refused_without_photos_worker_signature_or_scan(self):
        # The order is the order the work happens in: photograph the
        # asset (§7), compare it with the intake record, then the two
        # signatures record what the comparison found.
        with self.assertRaisesMessage(ValidationError, "Photograph the asset"):
            sign_handover(self.handover, "Jane Smith")
        _attach_handover_photo(self.handover, self.user)
        with self.assertRaisesMessage(ValidationError, "responsible"):
            sign_handover(self.handover, "Jane Smith")
        sign_handover_technician(self.handover, self.user)
        with self.assertRaisesMessage(ValidationError, "approval scan"):
            sign_handover(self.handover, "Jane Smith")

    def test_sign_handover_sets_name_and_timestamp(self):
        _make_handover_signable(self.handover, self.user)
        sign_handover(self.handover, "Jane Smith")
        self.handover.refresh_from_db()
        self.assertEqual(self.handover.received_by_name, "Jane Smith")
        self.assertIsNotNone(self.handover.signed_at)

    def test_re_signing_is_refused(self):
        _make_handover_signable(self.handover, self.user)
        sign_handover(self.handover, "Jane Smith")
        first_signed_at = self.handover.signed_at
        with self.assertRaises(ValidationError):
            sign_handover(self.handover, "Someone Else")
        self.handover.refresh_from_db()
        self.assertEqual(self.handover.received_by_name, "Jane Smith")
        self.assertEqual(self.handover.signed_at, first_signed_at)

    def test_signing_writes_system_activity_log(self):
        _make_handover_signable(self.handover, self.user)
        sign_handover(self.handover, "Jane Smith")
        entry = ActivityLog.objects.filter(
            case=self.handover.case, text__contains="receipt confirmed"
        ).get()
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("Jane Smith", entry.text)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CreateHandoverRecordTests(TestCase):
    """SVC-18: one unsigned handover record per case at a time."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tech", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="ready_for_handover",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        # SVC-18: the handover record hands back an asset the work is finished
        # on, so a service case needs the signed final work order before
        # one can be drawn up at all.
        from django.utils import timezone

        from apps.service.models import WorkOrder

        WorkOrder.objects.create(
            case=self.case, is_preliminary=False, signed_at=timezone.now()
        )

    def test_create_sets_author_note_and_logs(self):
        handover = create_handover_record(
            self.case, self.user, "Fender missing."
        )
        self.assertEqual(handover.case, self.case)
        self.assertEqual(handover.handed_over_by, self.user)
        self.assertEqual(handover.discrepancies_note, "Fender missing.")
        self.assertIsNone(handover.signed_at)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("handover record #%s" % handover.pk, entry.text)

    def test_second_unsigned_record_is_refused(self):
        create_handover_record(self.case, self.user)
        with self.assertRaises(ValidationError):
            create_handover_record(self.case, self.user)
        self.assertEqual(HandoverRecord.objects.count(), 1)

    def test_create_allowed_after_previous_record_is_signed(self):
        first = create_handover_record(self.case, self.user)
        _make_handover_signable(first, self.user)
        sign_handover(first, "Jane Smith")
        second = create_handover_record(self.case, self.user)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(HandoverRecord.objects.count(), 2)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class UpdateHandoverNoteTests(TestCase):
    """SVC-18: the discrepancies note is editable only until signing."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="tech", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="ready_for_handover",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.handover = HandoverRecord.objects.create(case=case)

    def test_note_updated_while_unsigned(self):
        update_handover_note(self.handover, "Scratch on port side.")
        self.handover.refresh_from_db()
        self.assertEqual(self.handover.discrepancies_note, "Scratch on port side.")

    def test_note_frozen_once_signed(self):
        _make_handover_signable(self.handover, self.user)
        sign_handover(self.handover, "Jane Smith")
        with self.assertRaises(ValidationError):
            update_handover_note(self.handover, "tampered")
        self.handover.refresh_from_db()
        self.assertEqual(self.handover.discrepancies_note, "")


def _attach_arrival_photo(case, user=None):
    """SVC-07 helper: put one INTAKE-stage photo on the case."""
    from apps.attachments.services import attach_file

    return attach_file(
        case,
        SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
        Attachment.Kind.PHOTO,
        Attachment.Stage.INTAKE,
        user=user,
    )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class CreateIntakeRecordTests(TestCase):
    """SVC-02: on a service case the work order precedes the intake record;
    SVC-06: one unsigned intake record per case at a time;
    SVC-07: the visible damage/issues description is mandatory."""

    def setUp(self):
        from apps.service.models import WorkOrder

        User = get_user_model()
        self.user = User.objects.create_user(username="fd", password="demo12345")
        self.status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="intake_scheduled",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
        )
        # §7.2: a receipt has to say which asset it is for.
        link_identified_asset(self.case)
        # SVC-02: the (preliminary) work order the intake record requires.
        WorkOrder.objects.create(case=self.case, is_preliminary=True)

    def test_create_sets_fields_and_logs(self):
        record = create_intake_record(
            self.case,
            self.user,
            mount_status=IntakeRecord.MountStatus.ON_MOUNT,
            cleanliness_status=IntakeRecord.CleanlinessStatus.CLEAN,
            storage_location="Hall 2",
            energy_level=IntakeRecord.EnergyLevel.HALF,
            damage_note="Scratch on bow.",
            general_condition_note="Otherwise OK.",
        )
        self.assertEqual(record.received_by, self.user)
        self.assertEqual(record.mount_status, IntakeRecord.MountStatus.ON_MOUNT)
        self.assertEqual(record.storage_location, "Hall 2")
        self.assertEqual(record.damage_note, "Scratch on bow.")
        self.assertIsNone(record.signed_at)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("intake record #%s" % record.pk, entry.text)

    def test_mount_status_is_mandatory(self):
        with self.assertRaisesMessage(ValidationError, "mount"):
            create_intake_record(self.case, self.user)
        self.assertEqual(IntakeRecord.objects.count(), 0)

    def test_damage_note_is_mandatory(self):
        # SVC-07: the arrival condition must be described explicitly -
        # "none" is the answer for an undamaged asset, not silence.
        with self.assertRaisesMessage(ValidationError, "SVC-07"):
            create_intake_record(
                self.case, self.user, mount_status="OFF_MOUNT",
                cleanliness_status="CLEAN", storage_location="Hall 1",
                damage_note="   ",
            )
        self.assertEqual(IntakeRecord.objects.count(), 0)

    def test_second_unsigned_record_is_refused(self):
        create_intake_record(
            self.case, self.user, mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN", storage_location="Hall 1",
            damage_note="none",
        )
        with self.assertRaises(ValidationError):
            create_intake_record(
                self.case, self.user, mount_status="OFF_MOUNT",
                cleanliness_status="CLEAN", storage_location="Hall 1",
                damage_note="none",
            )
        self.assertEqual(IntakeRecord.objects.count(), 1)

    def test_create_allowed_after_previous_record_is_signed(self):
        first = create_intake_record(
            self.case, self.user, mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN", storage_location="Hall 1",
            damage_note="none", energy_level="HALF",  # §7.6
        )
        _attach_arrival_photo(self.case, self.user)
        confirm_no_intake_items(first, self.user)
        sign_intake_record(first, self.user)
        second = create_intake_record(
            self.case, self.user, mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN", storage_location="Hall 1",
            damage_note="none",
        )
        self.assertNotEqual(first.pk, second.pk)

    def test_refused_before_work_order_on_service_case(self):
        # SVC-02: the wizard opens the work order first; an intake record on
        # a service case without one is a procedure inversion.
        bare = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
        )
        with self.assertRaisesMessage(ValidationError, "SVC-02"):
            create_intake_record(
                bare, self.user, mount_status="OFF_MOUNT",
                cleanliness_status="CLEAN", storage_location="Hall 1",
                damage_note="none",
            )
        self.assertEqual(IntakeRecord.objects.count(), 0)

    def test_non_service_case_needs_no_work_order(self):
        # Transport legs and site pickups record intake too; those
        # case types have no work-order concept.
        status = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="new_request",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        record = create_intake_record(
            case, self.user, mount_status="ON_MOUNT",
            cleanliness_status="CLEAN", damage_note="none",
        )
        self.assertIsNotNone(record.pk)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SignIntakeRecordTests(TestCase):
    """SVC-05/SVC-06: staff receipt confirmation, once only, attributed
    to the responsible technician when one is assigned."""

    def setUp(self):
        User = get_user_model()
        self.staffer = User.objects.create_user(
            username="fd2", password="demo12345", role="FRONT_DESK"
        )
        self.tech = User.objects.create_user(
            username="tech2", password="demo12345", role="TECHNICIAN"
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="intake_scheduled",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        # §7.2: a receipt has to say which asset it is for.
        link_identified_asset(self.case)
        self.record = IntakeRecord.objects.create(
            case=self.case,
            mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN",
            storage_location="Hall 1",
            damage_note="none",
            # §7.6: the intake record reads the gauge.
            energy_level="HALF",
            # SVC-06: the inventory has to be answered before a receipt
            # can be signed; nothing came with the asset.
            no_items_received=True,
        )
        _attach_arrival_photo(self.case, self.staffer)

    def test_sign_refused_without_arrival_photo(self):
        # SVC-07: a video alone does not satisfy the photo requirement.
        from apps.attachments.services import attach_file

        Attachment.objects.all().delete()
        attach_file(
            self.case,
            SimpleUploadedFile("arrival.mp4", b"fake-video"),
            Attachment.Kind.VIDEO,
            Attachment.Stage.INTAKE,
            user=self.staffer,
        )
        with self.assertRaisesMessage(ValidationError, "SVC-07"):
            sign_intake_record(self.record, self.staffer)
        self.record.refresh_from_db()
        self.assertIsNone(self.record.signed_at)

    def test_sign_falls_back_to_responsible_technician(self):
        self.case.assigned_technician = self.tech
        self.case.save(update_fields=["assigned_technician"])
        sign_intake_record(self.record, self.staffer)
        self.record.refresh_from_db()
        self.assertEqual(self.record.signed_by, self.tech)
        self.assertIsNotNone(self.record.signed_at)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertIn("recorded by '%s'" % self.staffer.username, entry.text)

    def test_sign_uses_acting_staffer_without_technician(self):
        sign_intake_record(self.record, self.staffer)
        self.record.refresh_from_db()
        self.assertEqual(self.record.signed_by, self.staffer)
        self.assertIsNotNone(self.record.signed_at)

    def test_re_signing_is_refused(self):
        sign_intake_record(self.record, self.staffer)
        first = self.record.signed_at
        with self.assertRaises(ValidationError):
            sign_intake_record(self.record, self.tech)
        self.record.refresh_from_db()
        self.assertEqual(self.record.signed_at, first)

    def test_missing_user_is_refused(self):
        with self.assertRaises(ValidationError):
            sign_intake_record(self.record, None)
        self.record.refresh_from_db()
        self.assertIsNone(self.record.signed_at)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class IntakeItemServiceTests(TestCase):
    """SVC-06 (procedure §3): categorized inventory managed on the
    unsigned record; the signed inventory is final."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="fd3", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="intake_scheduled",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        # §7.2: a receipt has to say which asset it is for.
        link_identified_asset(self.case)
        self.record = IntakeRecord.objects.create(
            case=self.case,
            mount_status="OFF_MOUNT",
            damage_note="none",
            energy_level="HALF",  # §7.6
        )

    def test_add_item_with_category_and_log(self):
        item = add_intake_item(
            self.record,
            self.user,
            category=IntakeItem.Category.KEY,
            item_name="  Ignition key  ",
            quantity=2,
            condition_note="worn",
        )
        self.assertEqual(item.category, IntakeItem.Category.KEY)
        self.assertEqual(item.item_name, "Ignition key")
        self.assertEqual(item.quantity, 2)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("Ignition key", entry.text)

    def test_items_ordered_by_category(self):
        add_intake_item(
            self.record, self.user, category="OTHER", item_name="Bucket"
        )
        add_intake_item(
            self.record, self.user, category="DOCUMENT", item_name="Registration"
        )
        add_intake_item(
            self.record, self.user, category="EQUIPMENT", item_name="Anchor"
        )
        self.assertEqual(
            [i.category for i in self.record.items.all()],
            ["DOCUMENT", "EQUIPMENT", "OTHER"],
        )

    def test_add_refuses_bad_input(self):
        with self.assertRaisesMessage(ValidationError, "category"):
            add_intake_item(
                self.record, self.user, category="WEAPONS", item_name="x"
            )
        with self.assertRaisesMessage(ValidationError, "item name"):
            add_intake_item(
                self.record, self.user, category="KEY", item_name="   "
            )
        with self.assertRaisesMessage(ValidationError, "quantity"):
            add_intake_item(
                self.record, self.user, category="KEY", item_name="Key", quantity=0
            )
        self.assertEqual(IntakeItem.objects.count(), 0)

    def test_signed_record_inventory_is_final(self):
        item = add_intake_item(
            self.record, self.user, category="EQUIPMENT", item_name="Anchor"
        )
        _attach_arrival_photo(self.case, self.user)
        sign_intake_record(self.record, self.user)
        with self.assertRaisesMessage(ValidationError, "amendment"):
            add_intake_item(
                self.record, self.user, category="KEY", item_name="Late key"
            )
        with self.assertRaises(ValidationError):
            remove_intake_item(item, self.user)
        self.assertEqual(IntakeItem.objects.count(), 1)

    def test_remove_item_while_unsigned(self):
        item = add_intake_item(
            self.record, self.user, category="PERSONAL", item_name="Sunglasses"
        )
        remove_intake_item(item, self.user)
        self.assertEqual(IntakeItem.objects.count(), 0)
        removal_log = ActivityLog.objects.filter(
            case=self.case, text__contains="removed"
        )
        self.assertTrue(removal_log.exists())
