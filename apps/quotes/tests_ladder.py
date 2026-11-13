"""Regressions from the 2026-11-04 module code review (quotes)."""

from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.accounts.models import ApprovalLimit

from .models import Approval, Quote
from .services import accept_fully_approved_quotes, record_approval
from .tests import make_case


class LadderChangeTests(TestCase):
    """A ladder edit must not strand a SENT quote: once every level it
    needs under the NEW limits is approved, nobody may decide on it again
    (INV-04), so it is accepted on the ladder change itself."""

    def setUp(self):
        for role, amount in [
            ("TECHNICIAN", "100"),
            ("MANAGER", "1000"),
            ("DIRECTOR", "100000"),
        ]:
            ApprovalLimit.objects.create(role=role, max_amount=Decimal(amount))
        self.quote = Quote.objects.create(
            case=make_case(),
            description="Engine overhaul",
            amount=Decimal("6000"),
            status=Quote.Status.SENT,
        )
        record_approval(
            self.quote, Approval.Level.CLIENT, Approval.Decision.APPROVED
        )
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_a_raised_limit_accepts_the_waiting_quote(self):
        ApprovalLimit.objects.filter(role="TECHNICIAN").update(
            max_amount=Decimal("10000")
        )
        promoted = accept_fully_approved_quotes()
        self.assertEqual([q.pk for q in promoted], [self.quote.pk])
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)

    def test_a_quote_still_missing_a_level_stays_sent(self):
        promoted = accept_fully_approved_quotes()
        self.assertEqual(promoted, [])
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_a_quote_without_client_consent_is_never_promoted(self):
        other = Quote.objects.create(
            case=self.quote.case,
            description="Unanswered",
            amount=Decimal("50"),
            status=Quote.Status.SENT,
        )
        accept_fully_approved_quotes()
        other.refresh_from_db()
        self.assertEqual(other.status, Quote.Status.SENT)

    def test_the_promotion_is_written_to_the_case_log(self):
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        director = get_user_model().objects.create_user(
            username="dir", password="x", role="DIRECTOR"
        )
        ApprovalLimit.objects.filter(role="TECHNICIAN").update(
            max_amount=Decimal("10000")
        )
        accept_fully_approved_quotes(user=director)
        entry = ActivityLog.objects.get(
            case=self.quote.case, text__contains="approval limits"
        )
        self.assertEqual(entry.author, director)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)

    def test_a_superseded_revision_is_not_promoted(self):
        """The client approved revision #1 at 6000; staff then issued a
        cheaper revision #2. A raised limit must not accept the old one."""
        Quote.objects.create(
            case=self.quote.case,
            description="Engine overhaul, revised",
            amount=Decimal("4500"),
            status=Quote.Status.SENT,
        )
        ApprovalLimit.objects.filter(role="TECHNICIAN").update(
            max_amount=Decimal("10000")
        )
        self.assertEqual(accept_fully_approved_quotes(), [])
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_a_quote_on_a_closed_case_is_left_alone(self):
        Status = django_apps.get_model("cases", "Status")
        closed = Status.objects.create(
            case_type=self.quote.case.case_type, code="closed", name="c",
            client_visible_name="c", is_terminal=True,
        )
        self.quote.case.status = closed
        self.quote.case.save(update_fields=["status"])
        ApprovalLimit.objects.filter(role="TECHNICIAN").update(
            max_amount=Decimal("10000")
        )
        self.assertEqual(accept_fully_approved_quotes(), [])
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)
