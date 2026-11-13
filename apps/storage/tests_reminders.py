"""WNT-03 storage-expiry reminders: who is due, what is sent, and the
once-per-agreement guarantee that makes a daily cron run safe."""
import datetime
from io import StringIO
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase

from .models import StorageAgreement
from .services import (
    agreements_due_for_expiry_reminder,
    send_expiry_reminders,
)
from .tests import build_agreement

TODAY = datetime.date(2027, 9, 15)


def with_period_end(agreement, end, *, email="", phone=""):
    """Give ``agreement`` a period end and its case a contactable client."""
    Client = django_apps.get_model("clients", "Client")
    agreement.period_end = end
    agreement.save(update_fields=["period_end"])
    if email or phone:
        client = Client.objects.create(name="Mark", email=email, phone=phone)
        agreement.case.client = client
        agreement.case.save(update_fields=["client"])
    return agreement


class DueForReminderTests(TestCase):
    def setUp(self):
        self.agreement = with_period_end(
            build_agreement("due"),
            TODAY + datetime.timedelta(days=20),
            email="mark@example.com",
        )

    def due(self, **kwargs):
        return list(agreements_due_for_expiry_reminder(today=TODAY, **kwargs))

    def test_period_inside_the_lead_time_is_due(self):
        self.assertEqual(self.due(), [self.agreement])

    def test_period_beyond_the_lead_time_is_not_due(self):
        self.agreement.period_end = TODAY + datetime.timedelta(days=45)
        self.agreement.save(update_fields=["period_end"])
        self.assertEqual(self.due(), [])
        # ...until the lead time is widened to cover it.
        self.assertEqual(self.due(days=60), [self.agreement])

    def test_period_already_over_is_not_due(self):
        # Too late to give renewal lead time; staff handle these by hand.
        self.agreement.period_end = TODAY - datetime.timedelta(days=1)
        self.agreement.save(update_fields=["period_end"])
        self.assertEqual(self.due(), [])

    def test_agreement_without_period_end_is_not_due(self):
        self.agreement.period_end = None
        self.agreement.save(update_fields=["period_end"])
        self.assertEqual(self.due(), [])

    def test_closed_case_is_not_due(self):
        status = self.agreement.case.status
        status.is_terminal = True
        status.save(update_fields=["is_terminal"])
        self.assertEqual(self.due(), [])

    def test_already_reminded_agreement_is_not_due(self):
        from django.utils import timezone

        self.agreement.expiry_reminder_sent_at = timezone.now()
        self.agreement.save(update_fields=["expiry_reminder_sent_at"])
        self.assertEqual(self.due(), [])

    def test_most_urgent_first(self):
        sooner = with_period_end(
            build_agreement("sooner"),
            TODAY + datetime.timedelta(days=3),
            email="ana@example.com",
        )
        self.assertEqual(self.due(), [sooner, self.agreement])


class SendExpiryRemindersTests(TestCase):
    def setUp(self):
        self.agreement = with_period_end(
            build_agreement("send"),
            datetime.date(2027, 10, 15),
            email="mark@example.com",
        )

    def activity(self, case):
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        return ActivityLog.objects.filter(case=case)

    def test_client_is_emailed_logged_and_stamped(self):
        counts = send_expiry_reminders(today=TODAY)

        self.assertEqual(counts, {"emailed": 1, "to_phone": 0, "failed": 0})
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["mark@example.com"])
        self.assertIn("15.10.2027", sent.subject)
        self.assertIn("15.10.2027", sent.body)
        self.assertIn("renewal offer", sent.body)
        # No tracking link - there is no client page, as in every other
        # client mail.
        self.assertNotIn("/track/", sent.body)
        self.assertNotIn(self.agreement.case.public_token, sent.body)

        # NTF-03: the attempt is recorded.
        EmailLog = django_apps.get_model("notifications", "EmailLog")
        self.assertTrue(
            EmailLog.objects.filter(
                case=self.agreement.case, success=True
            ).exists()
        )
        entry = self.activity(self.agreement.case).get()
        self.assertEqual(entry.kind, "SYSTEM")
        self.assertIn("expiry reminder", entry.text)

        self.agreement.refresh_from_db()
        self.assertIsNotNone(self.agreement.expiry_reminder_sent_at)

    def test_second_run_sends_nothing(self):
        # The point of the stamp: a daily cron never mails the same client
        # about the same period twice.
        send_expiry_reminders(today=TODAY)
        counts = send_expiry_reminders(today=TODAY)

        self.assertEqual(counts, {"emailed": 0, "to_phone": 0, "failed": 0})
        self.assertEqual(len(mail.outbox), 1)

    def test_package_named_in_the_body(self):
        StoragePackage = django_apps.get_model("storage", "StoragePackage")
        self.agreement.package = StoragePackage.objects.create(
            name="Paket Storage",
            season_start=datetime.date(2026, 10, 15),
            season_end=datetime.date(2027, 10, 15),
        )
        self.agreement.save(update_fields=["package"])

        send_expiry_reminders(today=TODAY)
        self.assertIn("Paket Storage", mail.outbox[0].body)

    def test_client_without_email_is_logged_for_a_phone_call(self):
        agreement = with_period_end(
            build_agreement("phone"),
            datetime.date(2027, 10, 1),
            phone="+44 67 123 456",
        )

        counts = send_expiry_reminders(today=TODAY)

        self.assertEqual(counts["to_phone"], 1)
        # Nothing was mailed to this case, but staff are told to call.
        entry = self.activity(agreement.case).get()
        self.assertIn("no client e-mail is on file", entry.text)
        self.assertNotIn(agreement.case.public_token, "".join(
            m.body for m in mail.outbox
        ))
        # Stamped as handled, so it surfaces once instead of every day.
        agreement.refresh_from_db()
        self.assertIsNotNone(agreement.expiry_reminder_sent_at)

    def test_send_failure_leaves_the_stamp_unset_for_a_retry(self):
        # A broken mail setup must not consume the one reminder the
        # agreement gets: no stamp means the next cron run tries again.
        failing_send = patch(
            "apps.notifications.services.send_mail",
            side_effect=OSError("smtp down"),
        )
        with self.assertLogs("apps.notifications.services", level="ERROR"):
            with failing_send:
                counts = send_expiry_reminders(today=TODAY)

        self.assertEqual(counts, {"emailed": 0, "to_phone": 0, "failed": 1})
        self.agreement.refresh_from_db()
        self.assertIsNone(self.agreement.expiry_reminder_sent_at)
        # No activity entry either: nothing happened to report.
        self.assertFalse(self.activity(self.agreement.case).exists())

    def test_dry_run_changes_nothing(self):
        counts = send_expiry_reminders(today=TODAY, dry_run=True)

        self.assertEqual(counts["emailed"], 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(self.activity(self.agreement.case).exists())
        self.agreement.refresh_from_db()
        self.assertIsNone(self.agreement.expiry_reminder_sent_at)


class SendStorageExpiryRemindersCommandTests(TestCase):
    def call(self, *args):
        out = StringIO()
        call_command("send_storage_expiry_reminders", *args, stdout=out)
        return out.getvalue()

    def test_reports_when_nothing_is_due(self):
        output = self.call()
        self.assertIn("No storage agreements end within 30 day(s)", output)
        self.assertEqual(len(mail.outbox), 0)

    def test_sends_and_reports_counts(self):
        # Due relative to the real today, since the command has no --today.
        with_period_end(
            build_agreement("cmd"),
            datetime.date.today() + datetime.timedelta(days=10),
            email="mark@example.com",
        )

        output = self.call()

        self.assertIn("Sent: 1 e-mail(s)", output)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            StorageAgreement.objects.filter(
                expiry_reminder_sent_at__isnull=False
            ).exists()
        )

    def test_dry_run_reports_without_sending(self):
        with_period_end(
            build_agreement("cmddry"),
            datetime.date.today() + datetime.timedelta(days=10),
            email="mark@example.com",
        )

        output = self.call("--dry-run")

        self.assertIn("Would send: 1 e-mail(s)", output)
        self.assertEqual(len(mail.outbox), 0)

    def test_negative_days_is_refused(self):
        with self.assertRaises(CommandError):
            self.call("--days", "-1")
