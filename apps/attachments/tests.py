"""Tests for the attachments app.

Heavy workflow tests live in the cases app; only smoke tests belong here.
"""

import shutil
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from .models import MAX_FILE_SIZE, Attachment, validate_file_size


def _jpeg_bytes(width=800, height=600, color=(120, 30, 30)):
    """Return the bytes of a solid-color JPEG of the given dimensions."""
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class _FakeFile:
    def __init__(self, size):
        self.size = size


class ThumbnailImagingTests(SimpleTestCase):
    """Pillow thumbnail generation (imaging.build_thumbnail_bytes)."""

    def test_downscales_large_image_to_max_edge(self):
        from io import BytesIO

        from PIL import Image

        from .imaging import THUMBNAIL_MAX_EDGE, build_thumbnail_bytes

        data = build_thumbnail_bytes(BytesIO(_jpeg_bytes(1000, 400)))
        self.assertIsNotNone(data)
        with Image.open(BytesIO(data)) as img:
            self.assertEqual(max(img.size), THUMBNAIL_MAX_EDGE)

    def test_returns_none_for_non_image_bytes(self):
        from io import BytesIO

        from .imaging import build_thumbnail_bytes

        self.assertIsNone(build_thumbnail_bytes(BytesIO(b"not-an-image")))


class AttachmentsSmokeTests(SimpleTestCase):
    def test_model_importable(self):
        self.assertTrue(Attachment)

    def test_validate_file_size_accepts_small_file(self):
        validate_file_size(_FakeFile(size=1024))  # should not raise

    def test_validate_file_size_rejects_oversized_file(self):
        with self.assertRaises(ValidationError):
            validate_file_size(_FakeFile(size=MAX_FILE_SIZE + 1))


class ProtectedMediaTests(TestCase):
    """POR-04: /media/ URLs require login and resist path traversal."""

    @classmethod
    def setUpClass(cls):
        # Must run before super().setUpClass(), which invokes setUpTestData.
        cls.media_root = tempfile.mkdtemp(prefix="test-media-")
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_root)
        cls.media_override.enable()
        cls.addClassCleanup(cls.media_override.disable)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.staff_user = get_user_model().objects.create_user(
            username="staff", password="secret", is_staff=True
        )
        file_path = Path(cls.media_root) / "cases" / "1" / "report" / "photo.jpg"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        cls.file_bytes = b"fake-jpeg-bytes"
        file_path.write_bytes(cls.file_bytes)
        cls.file_url = "/media/cases/1/report/photo.jpg"

    def test_anonymous_request_redirects_to_login(self):
        response = self.client.get(self.file_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_authenticated_staff_gets_file_bytes(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(self.file_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.file_bytes)

    def test_file_response_carries_immutable_cache_control(self):
        # Stored names are UUIDs, so the bytes never change: cache hard,
        # but privately (auth-protected client property).
        self.client.force_login(self.staff_user)
        response = self.client.get(self.file_url)
        self.assertEqual(
            response["Cache-Control"], "private, max-age=31536000, immutable"
        )

    def test_path_traversal_returns_404(self):
        self.client.force_login(self.staff_user)
        response = self.client.get("/media/../settings")
        self.assertEqual(response.status_code, 404)

    def test_missing_file_returns_404(self):
        self.client.force_login(self.staff_user)
        response = self.client.get("/media/cases/1/report/nope.jpg")
        self.assertEqual(response.status_code, 404)

    @override_settings(MEDIA_ACCEL_REDIRECT=True)
    def test_accel_redirect_mode_answers_with_header(self):
        # POR-04 production path: auth stays in Django, nginx streams the
        # file from the internal location.
        self.client.force_login(self.staff_user)
        response = self.client.get(self.file_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["X-Accel-Redirect"],
            "/internal-media/cases/1/report/photo.jpg",
        )
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response.content, b"")
        self.assertEqual(
            response["Cache-Control"], "private, max-age=31536000, immutable"
        )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AttachFileServiceTests(TestCase):
    """INV-08/POR-04: model validators are the shared upload rules."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.cases.models import Case, CaseType, ReportChannel, Status

        self.SimpleUploadedFile = SimpleUploadedFile
        User = get_user_model()
        self.user = User.objects.create_user(username="fd", password="demo12345")
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

    def test_attach_stores_uuid_name_and_keeps_original(self):
        from .services import attach_file

        attachment = attach_file(
            self.case,
            self.SimpleUploadedFile("Bow Damage.JPG", b"fake-jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.WORK,
            user=self.user,
        )
        self.assertEqual(attachment.original_name, "Bow Damage.JPG")
        self.assertEqual(attachment.uploaded_by, self.user)
        self.assertNotIn("Bow Damage", attachment.file.name)
        self.assertTrue(attachment.file.name.endswith(".jpg"))
        self.assertIn("cases/%s/work/" % self.case.pk, attachment.file.name)

    def test_disallowed_extension_is_rejected(self):
        from .services import attach_file

        with self.assertRaises(ValidationError):
            attach_file(
                self.case,
                self.SimpleUploadedFile("evil.exe", b"MZ"),
                Attachment.Kind.DOCUMENT,
                Attachment.Stage.WORK,
            )
        self.assertEqual(Attachment.objects.count(), 0)

    def test_oversized_file_is_rejected(self):
        from .services import attach_file

        big = self.SimpleUploadedFile("big.jpg", b"x")
        big.size = MAX_FILE_SIZE + 1
        with self.assertRaises(ValidationError):
            attach_file(
                self.case, big, Attachment.Kind.PHOTO, Attachment.Stage.WORK
            )
        self.assertEqual(Attachment.objects.count(), 0)

    def test_unknown_stage_is_rejected(self):
        from .services import attach_file

        with self.assertRaises(ValidationError):
            attach_file(
                self.case,
                self.SimpleUploadedFile("a.jpg", b"x"),
                Attachment.Kind.PHOTO,
                "vacation",
            )

    def test_attachments_for_scopes_to_object(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

        from .services import attach_file, attachments_for

        other_status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="other",
            name="o",
            client_visible_name="o",
        )
        other_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=other_status,
        )
        mine = attach_file(
            self.case,
            self.SimpleUploadedFile("a.jpg", b"x"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.WORK,
        )
        attach_file(
            other_case,
            self.SimpleUploadedFile("b.jpg", b"x"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.WORK,
        )
        self.assertEqual(list(attachments_for(self.case)), [mine])

    def test_photo_upload_generates_thumbnail_beside_original(self):
        from PIL import Image

        from .imaging import THUMBNAIL_MAX_EDGE
        from .services import attach_file

        attachment = attach_file(
            self.case,
            self.SimpleUploadedFile("wide.jpg", _jpeg_bytes(1200, 800)),
            Attachment.Kind.PHOTO,
            Attachment.Stage.WORK,
        )
        self.assertTrue(attachment.thumbnail)
        base = attachment.file.name.rsplit(".", 1)[0]
        self.assertEqual(attachment.thumbnail.name, base + "_thumb.jpg")
        with Image.open(attachment.thumbnail.path) as img:
            self.assertEqual(max(img.size), THUMBNAIL_MAX_EDGE)

    def test_document_upload_has_no_thumbnail(self):
        from .services import attach_file

        attachment = attach_file(
            self.case,
            self.SimpleUploadedFile("spec.pdf", b"%PDF-1.4 fake"),
            Attachment.Kind.DOCUMENT,
            Attachment.Stage.WORK,
        )
        self.assertFalse(attachment.thumbnail)

    def test_undecodable_photo_skips_thumbnail_but_upload_survives(self):
        from .services import attach_file

        attachment = attach_file(
            self.case,
            self.SimpleUploadedFile("broken.jpg", b"not-a-real-image"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.WORK,
        )
        self.assertIsNotNone(attachment.pk)
        self.assertFalse(attachment.thumbnail)

    def test_backfill_command_fills_missing_thumbnail(self):
        from io import StringIO

        from django.core.management import call_command

        from .services import attach_file

        attachment = attach_file(
            self.case,
            self.SimpleUploadedFile("old.jpg", _jpeg_bytes()),
            Attachment.Kind.PHOTO,
            Attachment.Stage.WORK,
        )
        # Simulate a photo uploaded before the feature existed.
        attachment.thumbnail.delete(save=False)
        attachment.thumbnail = ""
        attachment.save(update_fields=["thumbnail"])

        out = StringIO()
        call_command("backfill_thumbnails", stdout=out)

        attachment.refresh_from_db()
        self.assertTrue(attachment.thumbnail)
        self.assertIn("Thumbnails created: 1", out.getvalue())
