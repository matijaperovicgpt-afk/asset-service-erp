"""Procedure §2 "Asset intake for service": the rules added 2026-08-20.

Each of these closes a place where the record could be signed while
saying nothing:

* §2.2 - the asset comes off its mount, so where it goes is recorded
  (`storage_location`), not optional.
* §2.3 - an empty inventory is an answer ("nothing was received"), never
  a silence, exactly as `damage_note` forces "none".
* §2.4 - whether a dirty asset was washed before it was photographed,
  because the arrival photos are only worth what that answer says; and
  the amendment path the procedure requires, which had no caller at all.
"""
import tempfile

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel, Status
from apps.clients.tests import link_identified_asset

from .models import IntakeRecord
from .services import (
    add_intake_item,
    append_intake_amendment,
    confirm_no_intake_items,
    create_intake_record,
    sign_intake_record,
)
from .tests import _attach_arrival_photo


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class IntakeRecordRulesTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="fd-s2", password="demo12345", role="FRONT_DESK"
        )
        self.status = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="s2_status",
            name="n",
            client_visible_name="n",
        )
        # A TRANSPORT case: intake records apply there too (WNT-05/TRP-06)
        # and it needs no work order, which keeps these tests about the
        # intake record rules alone.
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.status,
        )
        # §7.2: a receipt has to say which asset it is for.
        link_identified_asset(self.case)

    def _create(self, **overrides):
        kwargs = {
            "mount_status": IntakeRecord.MountStatus.ON_MOUNT,
            "cleanliness_status": IntakeRecord.CleanlinessStatus.CLEAN,
            "damage_note": "none",
            # §7.6: the gauge is read before the receipt is signed.
            "energy_level": IntakeRecord.EnergyLevel.HALF,
        }
        kwargs.update(overrides)
        return create_intake_record(self.case, self.user, **kwargs)

    # --- §2.4 wash status ---------------------------------------------
    def test_cleanliness_status_is_mandatory(self):
        with self.assertRaisesMessage(ValidationError, "SVC-07"):
            self._create(cleanliness_status="")
        self.assertEqual(IntakeRecord.objects.count(), 0)

    def test_cleanliness_status_is_stored(self):
        record = self._create(cleanliness_status=IntakeRecord.CleanlinessStatus.CLEANED)
        self.assertEqual(record.cleanliness_status, IntakeRecord.CleanlinessStatus.CLEANED)

    def test_an_unknown_cleanliness_status_is_refused(self):
        with self.assertRaises(ValidationError):
            self._create(cleanliness_status="RINSED")

    # --- §2.2 storage location ----------------------------------------
    def test_storage_location_required_off_the_mount(self):
        with self.assertRaisesMessage(ValidationError, "where it will be stored"):
            self._create(
                mount_status=IntakeRecord.MountStatus.OFF_MOUNT
            )
        self.assertEqual(IntakeRecord.objects.count(), 0)

        record = self._create(
            mount_status=IntakeRecord.MountStatus.OFF_MOUNT,
            storage_location="Hall 2",
        )
        self.assertEqual(record.storage_location, "Hall 2")

    def test_storage_location_stays_optional_on_the_mount(self):
        record = self._create(
            mount_status=IntakeRecord.MountStatus.ON_MOUNT
        )
        self.assertEqual(record.storage_location, "")

    # --- §2.3 the inventory is answered, not left silent ---------------
    def test_signing_is_refused_while_the_inventory_is_unanswered(self):
        record = self._create()
        _attach_arrival_photo(self.case, self.user)
        with self.assertRaisesMessage(ValidationError, "nothing was received"):
            sign_intake_record(record, self.user)
        record.refresh_from_db()
        self.assertIsNone(record.signed_at)

    def test_one_listed_item_answers_the_inventory(self):
        record = self._create()
        _attach_arrival_photo(self.case, self.user)
        add_intake_item(
            record, self.user, category="KEY", item_name="Ignition key"
        )
        sign_intake_record(record, self.user)
        record.refresh_from_db()
        self.assertIsNotNone(record.signed_at)

    def test_nothing_received_answers_the_inventory_and_logs(self):
        record = self._create()
        _attach_arrival_photo(self.case, self.user)
        confirm_no_intake_items(record, self.user)
        record.refresh_from_db()
        self.assertTrue(record.no_items_received)
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="nothing was received"
            ).exists()
        )
        sign_intake_record(record, self.user)
        record.refresh_from_db()
        self.assertIsNotNone(record.signed_at)

    def test_nothing_received_is_refused_while_items_are_listed(self):
        record = self._create()
        add_intake_item(
            record, self.user, category="EQUIPMENT", item_name="Anchor"
        )
        with self.assertRaisesMessage(ValidationError, "cannot also be true"):
            confirm_no_intake_items(record, self.user)
        record.refresh_from_db()
        self.assertFalse(record.no_items_received)

    def test_nothing_received_is_refused_on_a_signed_record(self):
        record = self._create()
        _attach_arrival_photo(self.case, self.user)
        confirm_no_intake_items(record, self.user)
        sign_intake_record(record, self.user)
        with self.assertRaisesMessage(ValidationError, "signed and final"):
            confirm_no_intake_items(record, self.user)

    # --- §2.4 amendments ----------------------------------------------
    def test_amendment_is_appended_and_logged(self):
        record = self._create()
        amendment = append_intake_amendment(
            record, "  Gelcoat crack found under the bow.  ", self.user
        )
        self.assertEqual(amendment.text, "Gelcoat crack found under the bow.")
        self.assertEqual(amendment.author, self.user)
        self.assertEqual(list(record.amendments.all()), [amendment])
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="amendment #%s" % amendment.pk
            ).exists()
        )

    def test_an_empty_amendment_is_refused(self):
        record = self._create()
        with self.assertRaisesMessage(ValidationError, "SVC-07"):
            append_intake_amendment(record, "   ", self.user)
        self.assertEqual(record.amendments.count(), 0)

    def test_amendments_survive_signing(self):
        """The whole point: a signed record is final, and later findings
        are appended to it rather than edited into it."""
        record = self._create()
        _attach_arrival_photo(self.case, self.user)
        confirm_no_intake_items(record, self.user)
        sign_intake_record(record, self.user)
        append_intake_amendment(record, "Scratch found on the frame.", self.user)
        record.refresh_from_db()
        self.assertEqual(record.damage_note, "none")  # untouched
        self.assertEqual(record.amendments.count(), 1)
