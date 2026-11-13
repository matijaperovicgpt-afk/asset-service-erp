"""Background delivery of notification e-mails (NTF-05).

The composition of each message is covered in ``tests.py``; this file
covers only the enqueue/execute split introduced when the sends moved off
the request path, and the one send that deliberately stayed synchronous.

"""

import json
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings

from apps.cases.models import Case, CaseType, Status
from apps.notifications import tasks
from apps.notifications.models import EmailLog
from apps.notifications.services import (
    notify_case_created,
    notify_case_transition,
    notify_new_portal_case,
    send_client_email,
)


class NotificationTaskTestBase(TestCase):
    """A SERVICE case whose next status notifies the client."""

    def setUp(self):
        self.old_status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
            sort_order=1,
        )
        self.new_status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_approval",
            name="Awaiting approval",
            client_visible_name="Awaiting your consent",
            sort_order=2,
            notify_client=True,
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel="PORTAL",
            raw_contact_name="Test Client",
            raw_phone="+44000000",
            raw_email="client@example.com",
            raw_asset_info="Test asset",
            symptoms_description="Engine noise",
            status=self.old_status,
        )


class NotificationEnqueueTests(NotificationTaskTestBase):
    """NTF-05: dispatchers run inline without a broker, queue with one."""

    def test_eager_mode_sends_inline_without_touching_the_broker(self):
        with patch.object(tasks.case_transition, "delay") as delay:
            notify_case_transition(self.case, self.old_status, self.new_status)
        delay.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailLog.objects.count(), 1)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_broker_mode_queues_only_after_commit(self):
        with patch.object(tasks.case_transition, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=False):
                notify_case_transition(self.case, self.old_status, self.new_status)
                # INV-13: nothing may reach the worker before the commit,
                # or it would query a row that is not written yet.
                delay.assert_not_called()
            with self.captureOnCommitCallbacks(execute=True):
                notify_case_transition(self.case, self.old_status, self.new_status)
        delay.assert_called_once_with(
            self.case.pk, self.old_status.pk, self.new_status.pk
        )
        # The request path itself sent nothing.
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_queued_arguments_are_json_serializable(self):
        """The broker carries JSON; a model instance would not survive."""
        with patch.object(tasks.case_transition, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                notify_case_transition(self.case, self.old_status, self.new_status)
        json.dumps(delay.call_args.args)

    def test_dispatcher_never_raises_for_a_missing_case(self):
        """notify_case_created(None) is an asserted contract in tests.py."""
        notify_case_created(None)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_a_broken_broker_cannot_break_the_caller(self):
        with patch.object(
            tasks.case_transition, "delay", side_effect=OSError("broker down")
        ):
            with self.captureOnCommitCallbacks(execute=True):
                notify_case_transition(self.case, self.old_status, self.new_status)


class NotificationTaskTests(NotificationTaskTestBase):
    """NTF-05: the task bodies resolve primary keys and deliver."""

    def test_case_transition_task_sends_the_client_mail(self):
        tasks.case_transition(self.case.pk, self.old_status.pk, self.new_status.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailLog.objects.count(), 1)

    @override_settings(STAFF_NOTIFICATION_EMAILS=["staff@example.com"])
    def test_new_portal_case_task_sends_the_staff_mail(self):
        tasks.new_portal_case(self.case.pk)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("staff@example.com", mail.outbox[0].to)

    def test_case_created_task_sends_the_confirmation(self):
        tasks.case_created(self.case.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_task_tolerates_a_deleted_case(self):
        """A case may be deleted between enqueue and execution."""
        stale_pk = self.case.pk
        self.case.delete()
        with self.assertLogs("apps.notifications.tasks", level="INFO"):
            tasks.case_transition(stale_pk, self.old_status.pk, self.new_status.pk)
        self.assertEqual(len(mail.outbox), 0)

    def test_transport_price_task_tolerates_a_deleted_case(self):
        stale_pk = self.case.pk
        self.case.delete()
        with self.assertLogs("apps.notifications.tasks", level="INFO"):
            tasks.transport_price_declared(stale_pk)
        self.assertEqual(len(mail.outbox), 0)


class SynchronousSendClientEmailTests(NotificationTaskTestBase):
    """NTF-05 carve-out: send_client_email keeps a real delivery receipt.

    SVC-17 stamps ``CompletionNotice.sent_at`` from this boolean and
    WNT-03 stamps ``expiry_reminder_sent_at``, where a failure must leave
    the stamp unset so the next cron run retries. "Queued" is not "sent".
    """

    def test_returns_true_and_logs_on_success(self):
        self.assertIs(send_client_email(self.case, "Subject", "Body"), True)
        self.assertTrue(EmailLog.objects.get().success)

    def test_returns_false_and_logs_the_failure(self):
        with patch(
            "apps.notifications.services.send_mail", side_effect=OSError("smtp down")
        ):
            self.assertIs(send_client_email(self.case, "Subject", "Body"), False)
        log = EmailLog.objects.get()
        self.assertFalse(log.success)
        self.assertIn("smtp down", log.error)

    def test_returns_false_without_an_address(self):
        self.case.raw_email = ""
        self.case.save(update_fields=["raw_email"])
        self.assertIs(send_client_email(self.case, "Subject", "Body"), False)

    @override_settings(CELERY_TASK_ALWAYS_EAGER=False)
    def test_is_never_queued_even_with_a_broker_configured(self):
        """The regression guard: this send must stay in-process."""
        self.assertIs(send_client_email(self.case, "Subject", "Body"), True)
        self.assertEqual(len(mail.outbox), 1)


class StaffNotificationEnqueueTests(NotificationTaskTestBase):
    """NTF-01: the portal's staff mail is the one the roadmap targeted."""

    @override_settings(
        CELERY_TASK_ALWAYS_EAGER=False, STAFF_NOTIFICATION_EMAILS=["staff@example.com"]
    )
    def test_portal_staff_mail_leaves_the_request_path(self):
        with patch.object(tasks.new_portal_case, "delay") as delay:
            with self.captureOnCommitCallbacks(execute=True):
                notify_new_portal_case(self.case)
        delay.assert_called_once_with(self.case.pk)
        self.assertEqual(len(mail.outbox), 0)
