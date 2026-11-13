"""Background thumbnail generation (INV-13).

``tests.py`` covers the thumbnail itself (size, EXIF, the undecodable
case); this file covers only where it runs - inline without a broker, in
a worker after commit with one - and the transaction hazard that made
the deferral necessary.

"""

import shutil
import tempfile
from io import BytesIO
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import TestCase, override_settings

from apps.attachments import tasks
from apps.attachments.models import Attachment
from apps.attachments.services import attach_file, enqueue_thumbnail


def _jpeg_bytes(width=1200, height=800, color=(120, 30, 30)):
    """Return the bytes of a solid-color JPEG of the given dimensions."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ThumbnailTaskTestBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

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

    def _photo(self, name="wide.jpg"):
        return SimpleUploadedFile(name, _jpeg_bytes())


class ThumbnailTaskTests(ThumbnailTaskTestBase):
    """The task resolves a primary key and delegates to the primitive."""

    def test_task_generates_a_thumbnail_for_a_pk(self):
        with patch("apps.attachments.services.enqueue_thumbnail"):
            attachment = attach_file(
                self.case,
                self._photo(),
                Attachment.Kind.PHOTO,
                Attachment.Stage.WORK,
            )
        self.assertFalse(attachment.thumbnail)

        self.assertIs(tasks.generate_thumbnail_task(attachment.pk), True)
        attachment.refresh_from_db()
        self.assertTrue(attachment.thumbnail)

    def test_task_is_idempotent(self):
        attachment = attach_file(
            self.case, self._photo(), Attachment.Kind.PHOTO, Attachment.Stage.WORK
        )
        # attach_file already made one (eager mode), so a re-run is a no-op.
        self.assertIs(tasks.generate_thumbnail_task(attachment.pk), False)

    def test_task_tolerates_a_deleted_attachment(self):
        """A row may be deleted between enqueue and execution."""
        attachment = attach_file(
            self.case, self._photo(), Attachment.Kind.PHOTO, Attachment.Stage.WORK
        )
        stale_pk = attachment.pk
        attachment.delete()
        with self.assertLogs("apps.attachments.tasks", level="INFO"):
            self.assertIs(tasks.generate_thumbnail_task(stale_pk), False)


class ThumbnailEnqueueTests(ThumbnailTaskTestBase):
    """INV-13: queued only after commit, inline when there is no broker."""

    def test_eager_mode_populates_the_instance_the_caller_holds(self):
        """The guard for the four AttachFileServiceTests cases.

        The task loads a second copy of the row, so the eager path has to
        call the primitive on the passed instance or ``attach_file``
        would return an attachment with an empty ``thumbnail``.
        """
        with patch.object(tasks.generate_thumbnail_task, "delay") as delay:
            attachment = attach_file(
                self.case,
                self._photo(),
                Attachment.Kind.PHOTO,
                Attachment.Stage.WORK,
            )
        delay.assert_not_called()
        self.assertTrue(attachment.thumbnail)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_broker_mode_defers_until_the_transaction_commits(self):
        """POR-01 attaches inside atomic(); a worker must not race it."""
        with patch.object(tasks.generate_thumbnail_task, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=False):
                with transaction.atomic():
                    attachment = attach_file(
                        self.case,
                        self._photo(),
                        Attachment.Kind.PHOTO,
                        Attachment.Stage.WORK,
                    )
                    delay.assert_not_called()
                delay.assert_not_called()

            with self.captureOnCommitCallbacks(execute=True):
                enqueue_thumbnail(attachment)
        delay.assert_called_once_with(attachment.pk)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_documents_never_queue_a_job(self):
        with patch.object(tasks.generate_thumbnail_task, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                attach_file(
                    self.case,
                    SimpleUploadedFile("manual.pdf", b"%PDF-1.4 fake"),
                    Attachment.Kind.DOCUMENT,
                    Attachment.Stage.WORK,
                )
        delay.assert_not_called()

    def test_an_existing_thumbnail_never_queues_a_job(self):
        # Built eagerly first: the override below must not suppress the
        # very thumbnail whose presence this test relies on.
        attachment = attach_file(
            self.case, self._photo(), Attachment.Kind.PHOTO, Attachment.Stage.WORK
        )
        self.assertTrue(attachment.thumbnail)
        with override_settings(CELERY_TASK_ALWAYS_EAGER=False):
            with patch.object(tasks.generate_thumbnail_task, "delay") as delay:
                with self.captureOnCommitCallbacks(execute=True):
                    enqueue_thumbnail(attachment)
        delay.assert_not_called()


class BackfillCommandTests(ThumbnailTaskTestBase):
    """The CLI repair path stays synchronous whatever the broker says."""

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_backfill_command_does_not_queue(self):
        from django.core.management import call_command

        with patch("apps.attachments.services.enqueue_thumbnail"):
            attachment = attach_file(
                self.case,
                self._photo(),
                Attachment.Kind.PHOTO,
                Attachment.Stage.WORK,
            )
        self.assertFalse(attachment.thumbnail)

        with patch.object(tasks.generate_thumbnail_task, "delay") as delay:
            call_command("backfill_thumbnails")
        delay.assert_not_called()
        attachment.refresh_from_db()
        self.assertTrue(attachment.thumbnail)
