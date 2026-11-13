"""Procedure §7 phase 2: *photos or video at intake and at handover*.

§7's seventh bullet names the photographs at both ends of the case in
one breath. Only the arrival half was ever asked for: ``sign_intake_record``
has refused receipt without an INTAKE-stage photo since SVC-07 was
implemented, while ``sign_handover`` checked two signatures and no
pictures at all. Worse, nothing in the application could produce a
HANDOVER-stage attachment, so the "Handover photos" count the comparison
card has always displayed was structurally zero.

Phase 5's other half, the fuel gauge, is below: §7.6 lists it beside
the items, keys and damage that the same signature already refused
unanswered.

Phases 1, 3 and 5's location live in ``apps/cases/tests_section7.py``;
phase 4 in ``apps/clients/tests_section7.py``.
"""
import tempfile

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.attachments.models import Attachment
from apps.attachments.services import attach_file
from apps.cases.models import Case, CaseType, ReportChannel, Status
from apps.intake.models import HandoverRecord, IntakeRecord
from apps.intake.services import (
    attach_handover_approval,
    compare_handover_with_intake,
    has_handover_photos,
    sign_handover,
    sign_handover_technician,
    sign_intake_record,
)


class HandoverPhotoMixin:
    """A SERVICE case at ``ready_for_handover`` with an open record."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="s7p2", password="demo12345", is_staff=True
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="ready_for_handover",
            name="Ready for handover",
            client_visible_name="Ready for handover",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.handover = HandoverRecord.objects.create(case=self.case)

    def photograph(
        self, name="handover.jpg", kind=Attachment.Kind.PHOTO, handover=None
    ):
        # Filed on the RECORD: a case can hold several handovers, and a
        # case-level photo let the first delivery's pictures stand in for
        # every one after it.
        return attach_file(
            handover or self.handover,
            SimpleUploadedFile(name, b"fake-bytes"),
            kind,
            Attachment.Stage.HANDOVER,
            user=self.user,
        )

    def countersign(self):
        """Everything ``sign_handover`` wants except the photographs."""
        sign_handover_technician(self.handover, self.user)
        attach_handover_approval(
            self.handover,
            SimpleUploadedFile("handover.pdf", b"%PDF-1.4 scan"),
            self.user,
        )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HasHandoverPhotosTests(HandoverPhotoMixin, TestCase):
    """The read that the guard, the guide and the card all share."""

    def test_false_on_a_case_with_nothing_attached(self):
        self.assertFalse(has_handover_photos(self.handover))

    def test_true_once_a_departure_photo_is_on_file(self):
        self.photograph()
        self.assertTrue(has_handover_photos(self.handover))

    def test_an_arrival_photo_does_not_count_as_a_departure_photo(self):
        # The whole point of §7 is that both ends are documented; a
        # single INTAKE photo satisfying both would defeat it.
        attach_file(
            self.case,
            SimpleUploadedFile("dolazak.jpg", b"fake-jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )
        self.assertFalse(has_handover_photos(self.handover))

    def test_a_video_alone_does_not_satisfy_it(self):
        # Same reading as the arrival half (SVC-07): the procedure says
        # "photos or video", and this application has always
        # read that as photographs required, video welcome. Splitting
        # the reading between the two ends would be worse than either.
        self.photograph(name="handover.mp4", kind=Attachment.Kind.VIDEO)
        self.assertFalse(has_handover_photos(self.handover))


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SignHandoverPhotoGuardTests(HandoverPhotoMixin, TestCase):
    """§7: the receiving signature waits for the pictures."""

    def test_signing_refused_without_departure_photos(self):
        self.countersign()
        with self.assertRaises(ValidationError) as ctx:
            sign_handover(self.handover, "Jane Smith")
        self.assertIn("Photograph the asset", str(ctx.exception))
        self.handover.refresh_from_db()
        self.assertIsNone(self.handover.signed_at)

    def test_the_photos_are_asked_for_before_either_signature(self):
        # Photograph, compare, then sign - so an empty record reports
        # the missing pictures, not the missing worker signature.
        with self.assertRaises(ValidationError) as ctx:
            sign_handover(self.handover, "Jane Smith")
        self.assertIn("Photograph the asset", str(ctx.exception))

    def test_signing_succeeds_once_everything_is_on_file(self):
        self.photograph()
        self.countersign()
        sign_handover(self.handover, "Jane Smith")
        self.handover.refresh_from_db()
        self.assertIsNotNone(self.handover.signed_at)
        self.assertEqual(self.handover.received_by_name, "Jane Smith")

    def test_the_comparison_card_finally_has_something_to_count(self):
        self.photograph()
        comparison = compare_handover_with_intake(self.handover)
        self.assertEqual(comparison["handover_photo_count"], 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HandoverPhotoScreenTests(HandoverPhotoMixin, TestCase):
    """It has to be uploadable from the case page, not only the shell."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "backoffice:handover-photos", args=[self.case.pk, self.handover.pk]
        )

    def test_the_card_uploads_photos_and_videos(self):
        response = self.client.post(
            self._url(),
            {
                "photos": SimpleUploadedFile("handover.jpg", b"fake-jpeg"),
                "videos": SimpleUploadedFile("handover.mp4", b"fake-video"),
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(has_handover_photos(self.handover))
        self.assertEqual(
            Attachment.objects.filter(
                stage=Attachment.Stage.HANDOVER, kind=Attachment.Kind.VIDEO
            ).count(),
            1,
        )

    def test_a_video_in_the_photo_field_is_refused(self):
        # Uploads cannot survive the POST-redirect-GET the case page
        # uses, so the round-trip shows the field as unanswered rather
        # than quoting the extension error; the rule itself is checked
        # on the form below.
        response = self.client.post(
            self._url(),
            {"photos": SimpleUploadedFile("handover.mp4", b"fake-video")},
            follow=True,
        )
        self.assertContains(response, "invalid-feedback")
        self.assertFalse(has_handover_photos(self.handover))

    def test_the_form_names_the_file_that_is_not_a_photo(self):
        from apps.backoffice.forms import HandoverMediaUploadForm

        form = HandoverMediaUploadForm(
            {},
            {"photos": [SimpleUploadedFile("handover.mp4", b"fake-video")]},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Not a photo", str(form.errors["photos"]))
        self.assertIn("handover.mp4", str(form.errors["photos"]))

    def test_the_upload_form_renders_on_the_case_page(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "handover/%s/photos/" % self.handover.pk)
        self.assertContains(response, "Handover photos missing")

    def test_the_badge_turns_once_photographed(self):
        self.photograph()
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "Handover photos on file")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class HandoverPhotoGuideTests(HandoverPhotoMixin, TestCase):
    """The wizard mirrors the guard rather than contradicting it."""

    def _step(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self.case.refresh_from_db()
        guide = build_guide(self.case, self.user, ctx_for(self.case))
        for step in guide["steps"]:
            if step.key == "upload_handover_photos":
                return step
        return None

    def test_the_step_is_listed_and_blocking_until_photographed(self):
        step = self._step()
        self.assertIsNotNone(step)
        self.assertFalse(step.done)
        self.assertTrue(step.blocking)

    def test_the_step_ticks_once_photographed(self):
        self.photograph()
        self.assertTrue(self._step().done)

    def test_the_step_drops_off_a_case_worked_where_the_asset_lies(self):
        # SVC-03: nothing was taken in, so nothing is handed back and
        # there is no departure to photograph.
        from apps.cases.models import WorkLocation
        from apps.cases.services import record_work_location_consult

        record_work_location_consult(self.case, WorkLocation.ON_SITE, self.user)
        self.assertIsNone(self._step())


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class FuelGaugeTests(TestCase):
    """§7.6: *list of items, equipment, keys, fuel and visible
    damage.*

    Four of those five were already refused unanswered - the inventory
    at the signature, the damage note at creation. Fuel was the one left
    out, and ``blank`` said only that nobody looked.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model

        from apps.clients.tests import link_identified_asset

        self.user = get_user_model().objects.create_user(
            username="s7p5f", password="demo12345", is_staff=True
        )
        self.status = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="s7p5_status",
            name="n",
            client_visible_name="n",
        )
        # TRANSPORT: intake records apply there too (TRP-06) and no work
        # order is needed, which keeps these tests about the gauge alone.
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.status,
        )
        link_identified_asset(self.case)
        self.record = IntakeRecord.objects.create(
            case=self.case,
            mount_status="ON_MOUNT",
            cleanliness_status="CLEAN",
            damage_note="none",
            no_items_received=True,
        )
        attach_file(
            self.case,
            SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )

    def test_signing_refused_while_the_gauge_is_unread(self):
        with self.assertRaises(ValidationError) as ctx:
            sign_intake_record(self.record, self.user)
        self.assertIn("fuel gauge", str(ctx.exception))
        self.record.refresh_from_db()
        self.assertIsNone(self.record.signed_at)

    def test_a_gauge_reading_opens_the_gate(self):
        self.record.energy_level = IntakeRecord.EnergyLevel.QUARTER
        self.record.save(update_fields=["energy_level"])
        sign_intake_record(self.record, self.user)
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.signed_at)

    def test_no_tank_is_also_an_answer(self):
        # A hand tool has no tank to read, and that is a fact about
        # the asset rather than a gap in the record.
        self.record.energy_level = IntakeRecord.EnergyLevel.NOT_APPLICABLE
        self.record.save(update_fields=["energy_level"])
        sign_intake_record(self.record, self.user)
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.signed_at)

    def test_empty_is_a_reading_not_an_absence(self):
        # EMPTY says there IS a tank and it is dry - a different
        # statement from "no tank", and from silence.
        self.record.energy_level = IntakeRecord.EnergyLevel.EMPTY
        self.record.save(update_fields=["energy_level"])
        sign_intake_record(self.record, self.user)
        self.record.refresh_from_db()
        self.assertIsNotNone(self.record.signed_at)
        self.assertNotEqual(
            IntakeRecord.EnergyLevel.EMPTY, IntakeRecord.EnergyLevel.NOT_APPLICABLE
        )

    def test_the_create_form_will_not_leave_the_gauge_blank(self):
        from apps.backoffice.forms import IntakeRecordCreateForm

        form = IntakeRecordCreateForm(
            {
                "mount_status": "ON_MOUNT",
                "cleanliness_status": "CLEAN",
                "damage_note": "none",
            },
            {"photos": [SimpleUploadedFile("a.jpg", b"x")]},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("energy_level", form.errors)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SecondHandoverPhotoTests(HandoverPhotoMixin, TestCase):
    """Phase-2 follow-up: photograph THIS handover.

    ``create_handover_record`` says in its own docstring that "a case can
    legitimately accumulate several, e.g. transport legs". The check was
    written against the case, so one photograph taken at the first
    delivery satisfied every delivery after it - the second asset went
    back on the first asset's pictures.
    """

    def _make_signable(self, handover):
        sign_handover_technician(handover, self.user)
        attach_handover_approval(
            handover, SimpleUploadedFile("handover.pdf", b"%PDF-1.4"), self.user
        )

    def test_the_second_record_is_not_satisfied_by_the_first_ones_photos(self):
        self.photograph()
        self._make_signable(self.handover)
        sign_handover(self.handover, "Anna Adams")

        second = HandoverRecord.objects.create(case=self.case)
        self._make_signable(second)
        self.assertFalse(has_handover_photos(second))
        with self.assertRaises(ValidationError) as ctx:
            sign_handover(second, "Anna Adams")
        self.assertIn("Photograph the asset", str(ctx.exception))

    def test_photographing_the_second_record_opens_its_gate(self):
        self.photograph()
        self._make_signable(self.handover)
        sign_handover(self.handover, "Anna Adams")

        second = HandoverRecord.objects.create(case=self.case)
        self._make_signable(second)
        self.photograph(name="handover2.jpg", handover=second)
        sign_handover(second, "Anna Adams")
        second.refresh_from_db()
        self.assertIsNotNone(second.signed_at)

    def test_the_photos_still_reach_the_case_gallery(self):
        # Filed on the record, but ``attachment_targets_for_case``
        # gathers the handover records, so nothing disappears from the
        # case file (INV-08) or the asset's media history.
        from apps.cases.services import case_attachments

        photo = self.photograph()
        self.assertIn(photo, list(case_attachments(self.case)))
