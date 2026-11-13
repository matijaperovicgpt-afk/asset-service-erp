"""Regressions from the 2026-11-04 module code review (backoffice)."""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ApprovalLimit
from apps.cases.models import Case, CaseType, ReportChannel
from apps.cases.tests import build_workflow
from apps.quotes.models import Approval, Quote
from apps.quotes.services import record_approval
from apps.quotes.tests import make_case

from .transport import TransportOfferForm


class CaseCreateDimensionTests(TestCase):
    """A storage inquiry with an impossible breadth is refused on the form;
    it used to 500 after the case (and its e-mail) were committed."""

    def setUp(self):
        build_workflow(CaseType.STORAGE, ["inquiry", "quote_sent"])
        self.client.force_login(
            get_user_model().objects.create_user(
                username="front", password="x", is_staff=True, role="FRONT_DESK"
            )
        )
        self.url = reverse("backoffice:case-create")
        self.data = {
            "case_type": CaseType.STORAGE,
            "report_channel": ReportChannel.PHONE,
            "contact_name": "Storage Client",
            "phone": "+44 67 555 111",
            "symptoms_description": "Storage inquiry.",
            "asset_length_m": "8",
            "asset_width_m": "9",
        }

    def test_width_over_length_is_a_form_error_and_creates_nothing(self):
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "cannot be larger than the length")
        self.assertFalse(Case.objects.exists())

    def test_implausible_length_is_a_form_error(self):
        response = self.client.post(
            self.url, {**self.data, "asset_length_m": "80", "asset_width_m": "3"}
        )
        self.assertContains(response, "not plausible")
        self.assertFalse(Case.objects.exists())


class TransportOfferFormTests(TestCase):
    """TRP-01 offers follow the quote rules (QA S-M2): no back-dated
    validity, no typing-accident amounts."""

    def test_past_validity_is_refused(self):
        form = TransportOfferForm(
            data={
                "description": "Tow",
                "amount": "500",
                "valid_until": (timezone.localdate() - timedelta(days=1)).isoformat(),
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("valid_until", form.errors)

    def test_an_absurd_amount_is_refused(self):
        form = TransportOfferForm(data={"description": "Tow", "amount": "9999999999.99"})
        self.assertFalse(form.is_valid())
        self.assertIn("amount", form.errors)

    def test_a_plain_offer_passes(self):
        form = TransportOfferForm(data={"description": "Tow", "amount": "500"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.fields["amount"].label, "Estimated cost (EUR)")


class LadderEditPromotesQuoteTests(TestCase):
    """Editing the ladder through the backoffice accepts a SENT quote that
    now needs nothing more (the mid-approval dead end)."""

    def setUp(self):
        self.rows = {
            role: ApprovalLimit.objects.create(role=role, max_amount=Decimal(amount))
            for role, amount in [
                ("TECHNICIAN", "100"),
                ("MANAGER", "1000"),
                ("DIRECTOR", "100000"),
            ]
        }
        self.quote = Quote.objects.create(
            case=make_case(), description="Overhaul",
            amount=Decimal("600"), status=Quote.Status.SENT,
        )
        record_approval(self.quote, Approval.Level.CLIENT, Approval.Decision.APPROVED)
        self.client.force_login(
            get_user_model().objects.create_user(
                username="dir", password="x", is_staff=True, role="DIRECTOR"
            )
        )

    def test_raising_the_technician_limit_accepts_the_quote(self):
        row = self.rows["TECHNICIAN"]
        prefix = "limit-%s" % row.pk
        response = self.client.post(
            reverse("backoffice:approval-limit-edit", args=[row.pk]),
            {"%s-role" % prefix: "TECHNICIAN", "%s-max_amount" % prefix: "900"},
            follow=True,
        )
        self.assertContains(response, "needs no further approval")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
