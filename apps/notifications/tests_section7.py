"""Procedure §7.1: where the client's e-mail actually goes.

The three roles §7.1 names - *reporter, owner and contact
person* - had two homes in this application: the ``raw_*`` trio for the
reporter and ``Case.client`` for the owner. ``_recipient_for`` knew both
and preferred the reporter, unconditionally and for the life of the
case. That preference outlived triage, so once staff matched a report to
its real owner every client-facing e-mail this application sends still
went to whoever filed the report - the status updates and the completion
notice (§7.9).

These tests are the routing half of phase 6; the designation itself is
tested in ``apps/cases/tests_section7.py``.
"""
from django.core import mail
from django.test import TestCase, override_settings

from apps.cases.models import Case, CaseType, ContactRole, Status
from apps.cases.services import record_contact_person
from apps.clients.models import Client
from apps.notifications.services import (
    client_email_for,
    client_phone_for,
    notify_case_transition,
    send_client_email,
)


class ContactRoutingMixin:
    """A portal report filed by one person about another person's asset."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="s7p6n", password="demo12345", is_staff=True
        )
        self.new_report = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
            sort_order=1,
        )
        self.notifying = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_approval",
            name="Awaiting approval",
            client_visible_name="Awaiting your consent",
            sort_order=2,
            notify_client=True,
        )
        # The site employee who noticed the smoke.
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel="PORTAL",
            status=self.new_report,
            raw_contact_name="Site Office",
            raw_phone="+44 32 000 000",
            raw_email="office@site.example.com",
        )
        # The owner, matched at triage - abroad, and the one who pays.
        self.owner = Client.objects.create(
            name="Anna Adams",
            phone="+44 68 999 888",
            email="ana@example.com",
        )
        self.case.client = self.owner
        self.case.save(update_fields=["client"])


class RecipientResolutionTests(ContactRoutingMixin, TestCase):
    """Who ``_recipient_for`` picks, through its two public accessors."""

    def test_an_undesignated_case_keeps_the_old_behaviour(self):
        # Deliberate: rows created before §7.1 was asked cannot grow a
        # designation retroactively, and they must keep reaching
        # somebody rather than nobody.
        self.assertEqual(client_email_for(self.case), "office@site.example.com")
        self.assertEqual(client_phone_for(self.case), "+44 32 000 000")

    def test_designating_the_owner_redirects_the_mail(self):
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.assertEqual(client_email_for(self.case), "ana@example.com")
        self.assertEqual(client_phone_for(self.case), "+44 68 999 888")

    def test_designating_the_reporter_keeps_the_mail_where_it_was(self):
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        self.assertEqual(client_email_for(self.case), "office@site.example.com")

    def test_a_representative_receives_it_instead_of_both(self):
        record_contact_person(
            self.case,
            self.user,
            role=ContactRole.REPRESENTATIVE,
            name="Helen Hill",
            email="helen@example.com",
        )
        self.assertEqual(client_email_for(self.case), "helen@example.com")

    def test_a_contact_reachable_only_by_phone_has_no_address(self):
        # Not an error: some people are only ever rung. The caller
        # already handles None by logging and sending nothing.
        record_contact_person(
            self.case,
            self.user,
            role=ContactRole.OTHER,
            name="Peter Price",
            phone="+44 69 555 444",
        )
        self.assertIsNone(client_email_for(self.case))
        self.assertEqual(client_phone_for(self.case), "+44 69 555 444")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DeliveredMailFollowsTheDesignationTests(ContactRoutingMixin, TestCase):
    """The routing is only worth anything if the mail actually moves."""

    def test_a_status_notification_goes_to_the_designated_contact(self):
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        mail.outbox = []
        notify_case_transition(self.case, self.new_report, self.notifying)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])

    def test_a_composed_client_email_goes_there_too(self):
        # ``send_client_email`` is the path used by the completion
        # notice (SVC-17) and the storage expiry reminder (WNT-03).
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        mail.outbox = []
        self.assertTrue(send_client_email(self.case, "Subject", "Body"))
        self.assertEqual(mail.outbox[0].to, ["ana@example.com"])

    def test_without_a_designation_it_still_reaches_the_reporter(self):
        mail.outbox = []
        notify_case_transition(self.case, self.new_report, self.notifying)
        self.assertEqual(mail.outbox[0].to, ["office@site.example.com"])
