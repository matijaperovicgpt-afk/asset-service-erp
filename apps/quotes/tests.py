"""Tests for the quotes app.

Heavy workflow tests (approval ladder, quote/case state interactions)
live in the cases app; only smoke tests belong here.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import ApprovalLimit

from .models import AdvancePayment, Approval, Quote
from .services import (
    create_quote,
    mark_advance_paid,
    missing_approval_levels,
    record_approval,
    request_advance_payment,
    required_approval_levels,
    send_quote,
)


def make_case():
    """Minimal Case for quote tests (mirrors the inline fixture above)."""
    from apps.cases.models import Case, CaseType, ReportChannel, Status

    status, _ = Status.objects.get_or_create(
        case_type=CaseType.SERVICE,
        code="new_report",
        defaults={"name": "n", "client_visible_name": "n"},
    )
    return Case.objects.create(
        case_type=CaseType.SERVICE,
        report_channel=ReportChannel.PHONE,
        status=status,
    )


class QuotesSmokeTests(SimpleTestCase):
    def test_models_importable(self):
        self.assertTrue(Quote)
        self.assertTrue(Approval)
        self.assertTrue(AdvancePayment)

    def test_quote_default_status(self):
        self.assertEqual(
            Quote._meta.get_field("status").default, Quote.Status.DRAFT
        )

    def test_advance_payment_default_percent(self):
        self.assertEqual(AdvancePayment._meta.get_field("percent").default, 50)


class RequiredApprovalLevelsTests(TestCase):
    """ACT-02 / SVC-12 / INV-04: approval ladder derived from ApprovalLimit."""

    def setUp(self):
        for role, amount in [
            ("TECHNICIAN", "100"),
            ("MANAGER", "1000"),
            ("DIRECTOR", "100000"),
        ]:
            ApprovalLimit.objects.create(role=role, max_amount=Decimal(amount))

    def test_within_technician_limit_needs_client_only(self):
        self.assertEqual(
            required_approval_levels(Decimal("100")), [Approval.Level.CLIENT]
        )

    def test_within_manager_limit_needs_manager(self):
        self.assertEqual(
            required_approval_levels(Decimal("500")),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )

    def test_above_manager_limit_needs_director(self):
        self.assertEqual(
            required_approval_levels(Decimal("5000")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_missing_limits_escalate_conservatively(self):
        ApprovalLimit.objects.all().delete()
        self.assertEqual(
            required_approval_levels(Decimal("10")),
            [Approval.Level.CLIENT, Approval.Level.DIRECTOR],
        )

    def test_missing_approval_levels_reflects_recorded_decisions(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        quote = Quote.objects.create(
            case=case, description="Repair", amount=Decimal("500")
        )
        self.assertEqual(
            missing_approval_levels(quote),
            [Approval.Level.CLIENT, Approval.Level.MANAGER],
        )
        Approval.objects.create(
            quote=quote,
            level=Approval.Level.CLIENT,
            decision=Approval.Decision.APPROVED,
        )
        self.assertEqual(missing_approval_levels(quote), [Approval.Level.MANAGER])
        # A rejection does not count as an approval.
        Approval.objects.create(
            quote=quote,
            level=Approval.Level.MANAGER,
            decision=Approval.Decision.REJECTED,
        )
        self.assertEqual(missing_approval_levels(quote), [Approval.Level.MANAGER])
        Approval.objects.create(
            quote=quote,
            level=Approval.Level.MANAGER,
            decision=Approval.Decision.APPROVED,
        )
        self.assertEqual(missing_approval_levels(quote), [])


class SendQuoteTests(TestCase):
    """SVC-12 / NTF-03 / ACT-02: sending a quote."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="frontdesk", role="FRONT_DESK"
        )
        self.case = make_case()
        self.quote = Quote.objects.create(
            case=self.case, description="Repair", amount=Decimal("500")
        )

    def test_draft_quote_becomes_sent_with_system_log_and_no_email(self):
        send_quote(self.quote, self.user)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)
        entry = self.case.activity_log.get()
        self.assertEqual(entry.kind, "SYSTEM")
        self.assertEqual(entry.author, self.user)
        self.assertEqual(
            entry.text,
            "Quote #%s sent, amount 500.00 EUR" % self.quote.pk,
        )
        # NTF-03: the client is notified by the case transition into
        # awaiting_approval (notify_client=True), not by send_quote.
        self.assertEqual(len(mail.outbox), 0)

    def test_non_draft_quote_refused(self):
        for status in [
            Quote.Status.SENT,
            Quote.Status.ACCEPTED,
            Quote.Status.REJECTED,
        ]:
            self.quote.status = status
            self.quote.save(update_fields=["status"])
            with self.assertRaises(ValidationError):
                send_quote(self.quote, self.user)

    def test_non_positive_amount_refused(self):
        for amount in [Decimal("0"), Decimal("-10")]:
            self.quote.amount = amount
            self.quote.save(update_fields=["amount"])
            with self.assertRaises(ValidationError):
                send_quote(self.quote, self.user)
            self.quote.refresh_from_db()
            self.assertEqual(self.quote.status, Quote.Status.DRAFT)


class RecordApprovalTests(TestCase):
    """ACT-02 / ACT-03 / INV-04: approval decisions and quote status."""

    def setUp(self):
        for role, amount in [
            ("TECHNICIAN", "100"),
            ("MANAGER", "1000"),
            ("DIRECTOR", "100000"),
        ]:
            ApprovalLimit.objects.create(role=role, max_amount=Decimal(amount))
        User = get_user_model()
        self.technician = User.objects.create_user(
            username="tech", role="TECHNICIAN"
        )
        self.manager = User.objects.create_user(
            username="manager", role="MANAGER"
        )
        self.director = User.objects.create_user(
            username="director", role="DIRECTOR"
        )
        self.superuser = User.objects.create_superuser(
            username="admin", email="", password="x"
        )
        self.case = make_case()

    def make_quote(self, amount):
        return Quote.objects.create(
            case=self.case,
            description="Repair",
            amount=Decimal(amount),
            status=Quote.Status.SENT,
        )

    def test_manager_level_authority_matrix(self):
        quote = self.make_quote("500")
        with self.assertRaises(ValidationError):
            record_approval(
                quote,
                Approval.Level.MANAGER,
                Approval.Decision.APPROVED,
                user=self.technician,
            )
        with self.assertRaises(ValidationError):
            record_approval(
                quote, Approval.Level.MANAGER, Approval.Decision.APPROVED
            )
        # A level with an APPROVED decision takes no repeat (INV-04), so
        # each authorized user decides on a fresh quote.
        for user in [self.manager, self.director, self.superuser]:
            approval = record_approval(
                self.make_quote("500"),
                Approval.Level.MANAGER,
                Approval.Decision.APPROVED,
                user=user,
            )
            self.assertEqual(approval.decided_by, user)
            self.assertIsNotNone(approval.decided_at)

    def test_director_level_authority_matrix(self):
        quote = self.make_quote("5000")
        for user in [self.technician, self.manager]:
            with self.assertRaises(ValidationError):
                record_approval(
                    quote,
                    Approval.Level.DIRECTOR,
                    Approval.Decision.APPROVED,
                    user=user,
                )
        for user in [self.director, self.superuser]:
            record_approval(
                self.make_quote("5000"),
                Approval.Level.DIRECTOR,
                Approval.Decision.APPROVED,
                user=user,
            )

    def test_client_approval_has_no_decided_by_and_names_recorder(self):
        quote = self.make_quote("100")
        approval = record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.APPROVED,
            user=self.manager,
            note="Confirmed by phone.",
        )
        # ACT-03: clients have no accounts.
        self.assertIsNone(approval.decided_by)
        self.assertEqual(
            approval.note, "Confirmed by phone. Recorded by manager."
        )

    def test_accepted_within_technician_limit_needs_client_only(self):
        quote = self.make_quote("100")
        record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)

    def test_accepted_only_when_internal_approval_also_present(self):
        quote = self.make_quote("500")
        record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)
        record_approval(
            quote,
            Approval.Level.MANAGER,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)

    def test_internal_approval_alone_does_not_accept(self):
        quote = self.make_quote("5000")
        record_approval(
            quote,
            Approval.Level.DIRECTOR,
            Approval.Decision.APPROVED,
            user=self.director,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)
        record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)

    def test_client_rejection_rejects_quote(self):
        quote = self.make_quote("500")
        record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.REJECTED,
            user=self.manager,
            note="Too expensive.",
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.REJECTED)

    def test_internal_rejection_leaves_status_unchanged(self):
        quote = self.make_quote("500")
        record_approval(
            quote,
            Approval.Level.MANAGER,
            Approval.Decision.REJECTED,
            user=self.manager,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.SENT)

    def test_repeat_client_approval_is_refused(self):
        # INV-04: one approval per level - the client cannot be recorded
        # as approving twice while the internal ladder is pending.
        quote = self.make_quote("500")
        record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        with self.assertRaises(ValidationError):
            record_approval(
                quote,
                Approval.Level.CLIENT,
                Approval.Decision.APPROVED,
                user=self.manager,
            )
        self.assertEqual(
            quote.approvals.filter(level=Approval.Level.CLIENT).count(), 1
        )

    def test_repeat_internal_approval_is_refused(self):
        quote = self.make_quote("500")
        record_approval(
            quote,
            Approval.Level.MANAGER,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        with self.assertRaises(ValidationError):
            record_approval(
                quote,
                Approval.Level.MANAGER,
                Approval.Decision.APPROVED,
                user=self.director,
            )

    def test_internal_rejection_may_be_reconsidered(self):
        # A rejection is not final on the internal ladder: the same level
        # may later be approved (only APPROVED decisions lock the level).
        quote = self.make_quote("500")
        record_approval(
            quote,
            Approval.Level.MANAGER,
            Approval.Decision.REJECTED,
            user=self.manager,
        )
        record_approval(
            quote,
            Approval.Level.MANAGER,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        self.assertEqual(
            quote.approvals.filter(level=Approval.Level.MANAGER).count(), 2
        )

    def test_not_required_level_is_refused(self):
        # ACT-02: a 5000 EUR quote needs DIRECTOR - recording a MANAGER
        # decision on it is refused (and vice versa for small amounts).
        quote = self.make_quote("5000")
        with self.assertRaises(ValidationError):
            record_approval(
                quote,
                Approval.Level.MANAGER,
                Approval.Decision.APPROVED,
                user=self.manager,
            )
        small = self.make_quote("100")
        with self.assertRaises(ValidationError):
            record_approval(
                small,
                Approval.Level.MANAGER,
                Approval.Decision.APPROVED,
                user=self.manager,
            )

    def test_settled_quote_takes_no_further_decisions(self):
        quote = self.make_quote("100")
        record_approval(
            quote,
            Approval.Level.CLIENT,
            Approval.Decision.APPROVED,
            user=self.manager,
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.ACCEPTED)
        with self.assertRaises(ValidationError):
            record_approval(
                quote,
                Approval.Level.CLIENT,
                Approval.Decision.REJECTED,
                user=self.manager,
            )
        rejected = self.make_quote("100")
        record_approval(
            rejected,
            Approval.Level.CLIENT,
            Approval.Decision.REJECTED,
            user=self.manager,
        )
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, Quote.Status.REJECTED)
        with self.assertRaises(ValidationError):
            record_approval(
                rejected,
                Approval.Level.CLIENT,
                Approval.Decision.APPROVED,
                user=self.manager,
            )


class RequestAdvancePaymentTests(TestCase):
    """Advance payment creation."""

    def setUp(self):
        self.case = make_case()
        self.quote = Quote.objects.create(
            case=self.case, description="Repair", amount=Decimal("1000.55")
        )

    def test_default_50_percent_of_quote_amount(self):
        payment = request_advance_payment(self.case, quote=self.quote)
        self.assertEqual(payment.percent, 50)
        self.assertEqual(payment.amount, Decimal("500.28"))
        self.assertEqual(payment.quote, self.quote)
        self.assertEqual(payment.case, self.case)

    def test_custom_percent_quantized_to_two_decimals(self):
        payment = request_advance_payment(
            self.case, quote=self.quote, percent=30
        )
        self.assertEqual(payment.amount, Decimal("300.17"))

    def test_explicit_amount_without_quote(self):
        payment = request_advance_payment(
            self.case, percent=50, amount=Decimal("250.00")
        )
        self.assertIsNone(payment.quote)
        self.assertEqual(payment.amount, Decimal("250.00"))

    def test_missing_amount_without_quote_refused(self):
        with self.assertRaises(ValidationError):
            request_advance_payment(self.case)


class CreateQuoteTests(TestCase):
    """SVC-11/SVC-12: quotes are drafted through the service."""

    def setUp(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

        User = get_user_model()
        self.user = User.objects.create_user(username="fd", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_approval",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )

    def test_creates_draft_quote_and_logs(self):
        from apps.cases.models import ActivityLog

        quote = create_quote(
            self.case, "Replace impeller.", Decimal("850.00"), user=self.user
        )
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        # OPEN-01: EUR-only currency policy - the default must be EUR.
        self.assertEqual(quote.currency, "EUR")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("quote #%s" % quote.pk, entry.text)

    def test_non_positive_amount_is_refused(self):
        with self.assertRaises(ValidationError):
            create_quote(self.case, "x", Decimal("0"))
        with self.assertRaises(ValidationError):
            create_quote(self.case, "x", None)
        self.assertEqual(Quote.objects.count(), 0)


class MarkAdvancePaidTests(TestCase):
    """TRP-04/INV-05: paid_at is stamped once."""

    def setUp(self):
        from apps.cases.models import Case, CaseType, ReportChannel, Status

        User = get_user_model()
        self.user = User.objects.create_user(username="fd", password="demo12345")
        status = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="advance_paid",
            name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        self.advance = AdvancePayment.objects.create(
            case=case, percent=50, amount=Decimal("500.00")
        )

    def test_marks_paid_once_and_logs(self):
        from apps.cases.models import ActivityLog

        mark_advance_paid(self.advance, self.user)
        self.advance.refresh_from_db()
        self.assertIsNotNone(self.advance.paid_at)
        entry = ActivityLog.objects.get(case=self.advance.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("marked paid", entry.text)

    def test_double_payment_is_refused(self):
        mark_advance_paid(self.advance, self.user)
        first = self.advance.paid_at
        with self.assertRaises(ValidationError):
            mark_advance_paid(self.advance, self.user)
        self.advance.refresh_from_db()
        self.assertEqual(self.advance.paid_at, first)
