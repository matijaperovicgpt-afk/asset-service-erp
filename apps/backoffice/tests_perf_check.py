"""Tests for ``manage.py perf_check``.

The command's whole purpose is to answer "is this feature actually on?"
after a deploy, so the behaviour worth pinning is its *verdict*: a
deliberately-off feature must not fail the check, and a feature that is
configured but broken must.
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings


def run_perf_check():
    """Return (stdout, exit_code). Exit code 0 means no inconsistency."""
    out = StringIO()
    try:
        call_command("perf_check", stdout=out, stderr=out)
    except SystemExit as exc:
        return out.getvalue(), exc.code
    return out.getvalue(), 0


class PerfCheckTests(TestCase):
    def test_all_features_off_is_not_a_failure(self):
        """Leaving Redis and accel-redirect unset is a supported configuration."""
        output, code = run_perf_check()
        self.assertEqual(code, 0)
        self.assertIn("MEDIA_ACCEL_REDIRECT", output)
        self.assertIn("No inconsistencies found", output)

    def test_reports_accel_redirect_when_enabled(self):
        with override_settings(MEDIA_ACCEL_REDIRECT=True):
            output, code = run_perf_check()
        self.assertEqual(code, 0)
        self.assertIn("/internal-media/", output)

    def test_warns_that_locmem_multiplies_the_rate_limit(self):
        """The consequence of no Redis that is a correctness bug, not a slowdown.

        django-ratelimit uses the default cache; with locmem each gunicorn
        worker keeps its own counter, so the effective limit is the
        configured rate times the worker count.
        """
        output, _ = run_perf_check()
        self.assertIn("django-ratelimit counters", output)
        self.assertIn("per worker process", output)

    def test_eager_celery_is_reported_as_off_not_broken(self):
        output, code = run_perf_check()
        self.assertEqual(code, 0)
        self.assertIn("eager", output)

    def test_unreachable_broker_is_a_failure(self):
        """Enqueuing into a broker nobody is listening to must not look healthy."""
        with override_settings(
            CELERY_TASK_ALWAYS_EAGER=False,
            CELERY_BROKER_URL="redis://127.0.0.1:65000/1",
        ):
            with patch("config.celery.app.connection") as conn:
                conn.return_value.__enter__.return_value.ensure_connection.side_effect = (
                    OSError("connection refused")
                )
                output, code = run_perf_check()
        self.assertEqual(code, 1)
        self.assertIn("enqueued", output)

    def test_broker_up_but_no_worker_is_a_failure(self):
        """A queue with no consumer grows without bound and nothing else says so."""
        with override_settings(
            CELERY_TASK_ALWAYS_EAGER=False,
            CELERY_BROKER_URL="redis://127.0.0.1:65000/1",
        ):
            with patch("config.celery.app.connection"), patch(
                "config.celery.app.control.ping", return_value=[]
            ):
                output, code = run_perf_check()
        self.assertEqual(code, 1)
        self.assertIn("no worker answered", output)

    def test_reports_thumbnail_backlog(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file
        from apps.cases.models import Case, CaseType, ReportChannel
        from apps.cases.tests import build_service_workflow

        statuses = build_service_workflow()
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=statuses["diagnostics"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Backlog",
        )
        # Undecodable bytes: attach_file stores the file but generates no
        # thumbnail, which is exactly the backlog state the command reports.
        attach_file(
            case,
            SimpleUploadedFile("x.jpg", b"not-an-image", content_type="image/jpeg"),
            kind=Attachment.Kind.PHOTO,
            stage=Attachment.Stage.INTAKE,
        )
        output, code = run_perf_check()
        self.assertEqual(code, 0)
        self.assertIn("backfill_thumbnails", output)

    def test_is_read_only(self):
        """It runs on production; it must never write."""
        from apps.attachments.models import Attachment

        before = Attachment.objects.count()
        run_perf_check()
        self.assertEqual(Attachment.objects.count(), before)
