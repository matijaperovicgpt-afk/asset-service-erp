"""Regression tests for three holes found by walking the service flow
adversarially through the rendered UI.

Each of these was reachable from a browser with an ordinary staff
session - no crafted requests, no developer tools:

1. A closed case accepted almost every write the case page offers. The
   state machine refuses to move a closed case anywhere, so "closed" is
   permanent; but everything that does not go through ``transition_case``
   ignored terminality entirely. A closed case could still be given a
   work order, an intake record, a quote, an appointment, a delivery method
   and a work location, and its settlement flag could be flipped back -
   leaving a closed case failing the very guard that let it close.

2. The handover record could be drawn up, signed by the technician
   and signed by the client on an asset nobody had touched. Worse, it
   stayed: ``_guard_closed`` accepts *any* signed handover on the case,
   so a record manufactured on day one satisfies the closing gate for
   good.

3. A quote could be created already expired. It could then be sent, and
   the client would find every button on it refused, with no way to say
   so.

What deliberately is NOT guarded, so that a later reader does not "fix"
it: marking a case financially settled early. Clients do prepay, the
flip is logged with its author, and closing still needs the signed
handover - so an early settlement buys nobody anything.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.forms import QuoteCreateForm
from apps.cases.models import Case, CaseType, ReportChannel
from apps.cases.tests import build_service_workflow
from apps.intake.models import HandoverRecord, IntakeRecord
from apps.intake.services import create_handover_record
from apps.quotes.models import Quote
from apps.scheduling.models import Appointment
from apps.service.models import WorkOrder

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _weekday(days_ahead=3):
    """A date the SVC-04 working-hours guard accepts."""
    day = timezone.localtime().date() + timedelta(days=days_ahead)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


class ClosedCaseIsReadOnlyTests(TestCase):
    """INV-15: a case in a terminal status takes no further changes."""

    def setUp(self):
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.statuses["closed"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Test Contact",
        )
        self.manager = get_user_model().objects.create_user(
            username="closed_mgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.client.force_login(self.manager)

    def _post(self, name, data=None, args=None):
        return self.client.post(
            reverse(name, args=args or [self.case.pk]), data or {}, follow=True
        )

    def test_work_order_cannot_be_opened(self):
        response = self._post("backoffice:work-order-create")
        self.assertFalse(WorkOrder.objects.filter(case=self.case).exists())
        self.assertContains(response, "is closed")

    def test_intake_record_cannot_be_filed(self):
        self._post(
            "backoffice:intake-create",
            {
                "mount_status": "ON_MOUNT",
                "cleanliness_status": "CLEAN",
                "damage_note": "none",
                "photos": SimpleUploadedFile(
                    "p.png", PNG, content_type="image/png"
                ),
            },
        )
        self.assertFalse(IntakeRecord.objects.filter(case=self.case).exists())

    def test_quote_cannot_be_created(self):
        self._post(
            "backoffice:quote-create",
            {
                "description": "late quote",
                "amount": "100.00",
                "valid_until": _weekday(30).isoformat(),
            },
        )
        self.assertFalse(Quote.objects.filter(case=self.case).exists())

    def test_appointment_cannot_be_booked(self):
        self._post(
            "backoffice:appointment-book",
            {
                "kind": "INTAKE",
                "scheduled_for_0": _weekday().isoformat(),
                "scheduled_for_1": "10:00",
            },
        )
        self.assertFalse(Appointment.objects.filter(case=self.case).exists())

    def test_delivery_method_cannot_be_changed(self):
        self._post(
            "backoffice:case-delivery-method",
            {"delivery_method": "CLIENT_SHIPS", "note": ""},
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.delivery_method, "")

    def test_work_location_cannot_be_changed(self):
        self._post(
            "backoffice:case-work-location",
            {"work_location": "ON_SITE", "consulted": "", "note": ""},
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.work_location, "")

    def test_settlement_cannot_be_flipped_back(self):
        """Closing requires settlement, so un-settling afterwards would
        leave a closed case failing its own closing guard."""
        self.case.financially_settled = True
        self.case.save(update_fields=["financially_settled"])
        self._post("backoffice:case-settlement", {"settled": "0", "note": ""})
        self.case.refresh_from_db()
        self.assertTrue(self.case.financially_settled)

    def test_attachments_are_still_accepted(self):
        """The one deliberate exception: paperwork arrives late, and
        filing it against the case it belongs to beats filing it
        nowhere. It is additive and it cannot change any guard's answer,
        since a closed case has no transitions left."""
        from apps.attachments.models import Attachment

        response = self._post(
            "backoffice:attachment-upload",
            {
                "kind": "photo",
                "stage": "handover",
                "file": SimpleUploadedFile(
                    "late.png", PNG, content_type="image/png"
                ),
            },
        )
        self.assertContains(response, "uploaded")
        self.assertTrue(
            Attachment.objects.filter(original_name="late.png").exists()
        )

    def test_detail_page_disables_every_section(self):
        """The server refuses these writes; the page must not keep
        offering them. One disabled fieldset per section makes every
        control inside inert without JavaScript, and is announced as
        disabled rather than discovered by submitting."""
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        html = response.content.decode()
        self.assertContains(response, "This case is closed")
        self.assertIn("<fieldset disabled", html)
        self.assertNotIn("<fieldset>", html)

    def test_closed_case_can_still_be_expanded_and_read(self):
        """The lock wraps section CONTENT, never the column. The toggle
        that opens a section is itself a <button>: a fieldset around the
        column disables it too, and the closed case - the one people
        come back to precisely in order to read it - can then never be
        opened. This is the regression that shipped and was caught by
        probing the storage flow, so it is pinned here."""
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        html = response.content.decode()
        toggles = html.count("guide-section-toggle")
        self.assertGreater(toggles, 0)
        # No toggle may sit between a "<fieldset disabled" and its "</fieldset>".
        for chunk in html.split("<fieldset disabled")[1:]:
            body = chunk.split("</fieldset>")[0]
            self.assertNotIn("guide-section-toggle", body)

    def test_open_case_sections_are_not_disabled(self):
        self.case.status = self.statuses["diagnostics"]
        self.case.save(update_fields=["status"])
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertNotContains(response, "<fieldset disabled")


class HandoverNeedsFinishedWorkTests(TestCase):
    """SVC-18: the handover record hands back an asset the work is finished on."""

    def setUp(self):
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.statuses["ready_for_handover"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Test Contact",
        )
        self.user = get_user_model().objects.create_user(
            username="handover_mgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )

    def test_refused_without_a_signed_work_order(self):
        with self.assertRaises(ValidationError) as caught:
            create_handover_record(self.case, self.user)
        self.assertIn("SVC-18", str(caught.exception))
        self.assertFalse(HandoverRecord.objects.filter(case=self.case).exists())

    def test_refused_while_the_work_order_is_only_preliminary(self):
        WorkOrder.objects.create(case=self.case, is_preliminary=True)
        with self.assertRaises(ValidationError):
            create_handover_record(self.case, self.user)

    def test_refused_while_the_final_order_is_unsigned(self):
        WorkOrder.objects.create(case=self.case, is_preliminary=False)
        with self.assertRaises(ValidationError):
            create_handover_record(self.case, self.user)

    def test_allowed_once_the_order_is_final_and_signed(self):
        WorkOrder.objects.create(
            case=self.case, is_preliminary=False, signed_at=timezone.now()
        )
        handover = create_handover_record(self.case, self.user)
        self.assertEqual(handover.case, self.case)

    def test_non_service_cases_are_unaffected(self):
        """A storage or transport case has no work order at all; the
        requirement is scoped to SERVICE exactly as ``_guard_closed`` is."""
        storage = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=self.statuses["ready_for_handover"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Storage Contact",
        )
        handover = create_handover_record(storage, self.user)
        self.assertEqual(handover.case, storage)


class QuoteValidUntilTests(TestCase):
    """A quote whose validity has already lapsed cannot be accepted by
    the client, so it must not be draftable."""

    def test_past_date_is_rejected(self):
        form = QuoteCreateForm(
            {
                "description": "expired",
                "amount": "10.00",
                "valid_until": "2020-01-01",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("valid_until", form.errors)

    def test_today_is_accepted(self):
        form = QuoteCreateForm(
            {
                "description": "fine",
                "amount": "10.00",
                "valid_until": timezone.localdate().isoformat(),
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_blank_stays_optional(self):
        form = QuoteCreateForm({"description": "fine", "amount": "10.00"})
        self.assertTrue(form.is_valid(), form.errors)
