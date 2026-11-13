"""Tests for the notifications app."""

from datetime import timedelta
from decimal import Decimal

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cases.models import ActivityLog, Case, CaseType, Status
from apps.notifications.models import EmailLog
from apps.notifications.services import (
    notify_case_created,
    notify_case_transition,
    notify_new_portal_case,
)


class NotifyCaseTransitionTests(TestCase):
    """NTF-02/NTF-03: client e-mails driven by Status.notify_client."""

    def setUp(self):
        self.new_report = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
            sort_order=1,
            is_terminal=False,
        )
        self.non_notify = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="in_progress",
            name="In progress",
            client_visible_name="Work in progress",
            sort_order=2,
            is_terminal=False,
        )
        self.notify = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_approval",
            name="Awaiting approval",
            client_visible_name="Awaiting your consent",
            sort_order=3,
            is_terminal=False,
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
            status=self.new_report,
        )

    def test_non_notify_status_does_nothing_and_never_raises(self):
        try:
            notify_case_transition(self.case, self.new_report, self.non_notify)
        except Exception as exc:  # pragma: no cover - the test IS the assertion
            self.fail(f"notify_case_transition raised unexpectedly: {exc}")
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_notify_client_flag_sends_email_and_logs(self):
        notify_case_transition(self.case, self.new_report, self.notify)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["client@example.com"])
        self.assertIn(self.notify.client_visible_name, message.body)
        # No client page exists, so no link and no token in the mail.
        self.assertNotIn(self.case.public_token, message.body)
        self.assertNotIn("/track/", message.body)
        log = EmailLog.objects.get()
        self.assertTrue(log.success)
        self.assertEqual(log.to_address, "client@example.com")

    def test_no_recipient_skips_silently(self):
        self.case.raw_email = ""
        self.case.save()
        notify_case_transition(self.case, self.new_report, self.notify)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)

    def test_status_mail_carries_no_price_or_appointment(self):
        """Prices and pickup times are staff's to send (the SVC-17
        completion notice, a call, an SMS) - the automatic status mail
        names the client-visible status and nothing else, even on the
        status that used to append them."""
        from django.apps import apps as django_apps

        ready = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="ready_for_handover",
            name="Ready",
            client_visible_name="Ready for pickup",
            sort_order=4,
            notify_client=True,
        )
        Quote = django_apps.get_model("quotes", "Quote")
        Quote.objects.create(
            case=self.case,
            description="Repair",
            amount=Decimal("450.00"),
            status=Quote.Status.ACCEPTED,
        )
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.HANDOVER,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        notify_case_transition(self.case, self.non_notify, ready)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Ready for pickup", body)
        self.assertNotIn("450.00", body)
        self.assertNotIn("EUR", body)
        self.assertNotIn("Pickup/delivery appointment", body)
        self.assertNotIn("Repair", body)


class NotifyCaseCreatedTests(TestCase):
    """NTF-04: creation confirmation for staff-entered cases."""

    def _make_case(self, **kwargs):
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
            sort_order=1,
        )
        defaults = {
            "case_type": CaseType.SERVICE,
            "report_channel": "PHONE",
            "raw_contact_name": "Test Client",
            "status": status,
        }
        defaults.update(kwargs)
        return Case.objects.create(**defaults)

    def test_email_on_file_sends_confirmation_without_a_link(self):
        case = self._make_case(raw_email="client@example.com")
        notify_case_created(case)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["client@example.com"])
        self.assertIn("Thank you.", message.body)
        # No client page exists, so nothing to link to.
        self.assertNotIn("/track/", message.body)
        self.assertNotIn(case.public_token, message.body)
        log = EmailLog.objects.get()
        self.assertTrue(log.success)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_phone_only_logs_staff_reminder_to_confirm_receipt(self):
        case = self._make_case(raw_phone="+44 67 123 456")
        notify_case_created(case)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)
        log = ActivityLog.objects.get()
        self.assertEqual(log.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("phone/SMS", log.text)
        self.assertIn("Confirm receipt", log.text)
        self.assertIn("+44 67 123 456", log.text)
        self.assertNotIn("/track/", log.text)
        self.assertNotIn(case.public_token, log.text)

    def test_never_raises(self):
        try:
            notify_case_created(None)  # forces a failure inside
        except Exception as exc:  # pragma: no cover - the test IS the assertion
            self.fail(f"notify_case_created raised unexpectedly: {exc}")


class NotifyNewPortalCaseTests(TestCase):
    """NTF-01: staff notified about every new portal submission."""

    def setUp(self):
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel="PORTAL",
            raw_contact_name="Test Client",
            raw_phone="+44000000",
            raw_asset_info="Test asset",
            symptoms_description="Engine noise",
            status=status,
        )

    @override_settings(
        STAFF_NOTIFICATION_EMAILS=["desk@example.com", "boss@example.com"]
    )
    def test_notifies_every_configured_staff_address(self):
        notify_new_portal_case(self.case)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            sorted(m.to[0] for m in mail.outbox),
            ["boss@example.com", "desk@example.com"],
        )
        self.assertIn("Test Client", mail.outbox[0].body)
        self.assertEqual(EmailLog.objects.filter(success=True).count(), 2)

    @override_settings(STAFF_NOTIFICATION_EMAILS=[])
    def test_no_recipients_configured_skips_silently(self):
        notify_new_portal_case(self.case)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)


class EmailLanguagePinningTests(TestCase):
    """NTF-03: notification e-mails always use settings.LANGUAGE_CODE,
    never the language active in the triggering request."""

    def setUp(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

        self.notify_status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="intake_scheduled",
            name="Zakazan prijem",
            client_visible_name="Zakazan prijem",
            notify_client=True,
        )
        self.old_status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PORTAL,
            status=self.notify_status,
            raw_email="client@example.com",
            raw_contact_name="Marko",
            raw_phone="067123456",
        )

    @override_settings(LANGUAGE_CODE="sr-latn")
    def test_client_mail_is_serbian_regardless_of_active_language(self):
        from django.core import mail
        from django.utils import translation

        from .services import notify_case_transition

        with translation.override("en"):
            notify_case_transition(self.case, self.old_status, self.notify_status)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Poštovani,", mail.outbox[0].body)
        self.assertIn("Novost o vašem servisnom zahtjevu", mail.outbox[0].subject)

    @override_settings(
        LANGUAGE_CODE="sr-latn",
        STAFF_NOTIFICATION_EMAILS=["staff@example.com"],
    )
    def test_staff_mail_is_serbian_regardless_of_active_language(self):
        from django.core import mail
        from django.utils import translation

        from .services import notify_new_portal_case

        with translation.override("en"):
            notify_new_portal_case(self.case)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Nova prijava sa portala", mail.outbox[0].subject)
        # §7.1: the staff mail names the REPORTER, which is what a
        # fresh submission actually knows - who we contact is a
        # designation staff make afterwards.
        self.assertIn("Prijavio: Marko", mail.outbox[0].body)


@override_settings(PORTAL_ENABLED=False)
class NoPortalNotificationTests(TestCase):
    """PORTAL_ENABLED only mounts or unmounts the report form: the client
    mails are the same in both modes, and never link anywhere."""

    def setUp(self):
        self.new_report = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
            sort_order=1,
        )
        self.notify = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_approval",
            name="Awaiting approval",
            client_visible_name="Awaiting your consent",
            sort_order=2,
            notify_client=True,
        )

    def _make_case(self, **kwargs):
        defaults = {
            "case_type": CaseType.SERVICE,
            "report_channel": "PHONE",
            "raw_contact_name": "Test Client",
            "status": self.new_report,
        }
        defaults.update(kwargs)
        return Case.objects.create(**defaults)

    def test_creation_confirmation_carries_no_tracking_link(self):
        case = self._make_case(raw_email="client@example.com")
        notify_case_created(case)
        body = mail.outbox[0].body
        self.assertNotIn("/track/", body)
        self.assertNotIn(case.public_token, body)
        self.assertIn("Thank you.", body)

    def test_phone_only_reminder_asks_to_confirm_receipt_not_share_a_link(self):
        case = self._make_case(raw_phone="+44 67 123 456")
        notify_case_created(case)
        log = ActivityLog.objects.get()
        self.assertIn("phone/SMS", log.text)
        self.assertIn("+44 67 123 456", log.text)
        self.assertNotIn("/track/", log.text)
        self.assertNotIn(case.public_token, log.text)

    def test_transition_mail_carries_no_tracking_link(self):
        case = self._make_case(raw_email="client@example.com")
        notify_case_transition(case, self.new_report, self.notify)
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        self.assertIn("Awaiting your consent", body)
        self.assertNotIn("/track/", body)
        self.assertNotIn(case.public_token, body)
