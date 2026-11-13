"""Regressions from the 2026-11-04 module code review (attachments access)."""

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.cases.models import Case, CaseType, ReportChannel, Status

from .models import Attachment
from .services import attach_file
from .tests import ProtectedMediaTests


class NonStaffMediaTests(ProtectedMediaTests):
    """A logged-in account without staff rights is refused everywhere
    else; the media view used to let it fetch any client photo by URL."""

    def test_non_staff_login_is_forbidden(self):
        self.client.force_login(
            get_user_model().objects.create_user(username="guest", password="x")
        )
        response = self.client.get(self.file_url)
        self.assertEqual(response.status_code, 403)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DerivedKindTests(TestCase):
    """The attachment kind follows the file's extension, not the caller's
    pick - a PDF tagged VIDEO used to be handed to ffmpeg."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        status = Status.objects.create(
            case_type=CaseType.SERVICE, code="new_report", name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE, report_channel=ReportChannel.PHONE, status=status,
        )

    def _attach(self, name, kind):
        return attach_file(
            self.case, SimpleUploadedFile(name, b"bytes"), kind, Attachment.Stage.REPORT
        )

    def test_a_pdf_tagged_video_is_a_document(self):
        self.assertEqual(self._attach("scan.pdf", Attachment.Kind.VIDEO).kind, "document")

    def test_a_jpeg_tagged_video_is_a_photo(self):
        self.assertEqual(self._attach("bow.jpg", Attachment.Kind.VIDEO).kind, "photo")

    def test_an_mp4_tagged_photo_is_a_video(self):
        self.assertEqual(self._attach("clip.mp4", Attachment.Kind.PHOTO).kind, "video")

    def test_a_pdf_tagged_photo_is_a_document(self):
        # The SVC-07 arrival-photo gate counts INTAKE photos: a scan
        # uploaded as "Photo" must not pass for one.
        self.assertEqual(self._attach("scan.pdf", Attachment.Kind.PHOTO).kind, "document")

    def test_a_jpeg_tagged_document_stays_a_document(self):
        # The caller's choice stands where the file allows it: a photographed
        # delivery note filed as a document must not count as an arrival photo.
        self.assertEqual(self._attach("note.jpg", Attachment.Kind.DOCUMENT).kind, "document")
