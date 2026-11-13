"""Tests for the backoffice app."""

import shutil
import tempfile
from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.backoffice.forms import AppointmentBookForm
from apps.backoffice.widgets import FiveMinuteDateTimeWidget
from apps.cases.models import ActivityLog, Case, ReportChannel, StatusHistory
from apps.cases.models import CaseType, Status, StatusTransition
from apps.cases.tests import build_service_workflow
from apps.clients.tests import link_identified_asset
from apps.intake.models import HandoverRecord, IntakeItem, IntakeRecord
from apps.parts.models import Part, PartOrder
from apps.quotes.models import Approval, Quote
from apps.scheduling import workinghours
from apps.scheduling.models import Appointment
from apps.service.models import Diagnostic, DiagnosticPart, WorkOrder


class CaseListViewTests(TestCase):
    def test_anonymous_request_redirects_to_login(self):
        from django.conf import settings

        response = self.client.get(reverse("backoffice:case-list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)

    def test_logged_in_staff_user_gets_200(self):
        user = get_user_model().objects.create_user(
            username="staffer",
            password="test-pass-123",
            is_staff=True,
        )
        self.client.force_login(user)
        response = self.client.get(reverse("backoffice:case-list"))
        self.assertEqual(response.status_code, 200)


class DiagnosticCreateViewTests(TestCase):
    """SVC-09: the diagnostic is recorded from the backoffice case page."""

    def setUp(self):
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.statuses["diagnostics"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Test Contact",
        )
        self.technician = get_user_model().objects.create_user(
            username="diag_tech", password="demo12345",
            is_staff=True, role="TECHNICIAN",
        )
        self.client.force_login(self.technician)
        self.url = reverse("backoffice:diagnostic-create", args=[self.case.pk])
        self.data = {
            "issue": "Impeller worn out.",
            "servicing_work": "Replace impeller, flush cooling circuit.",
            "parts_needed": "Impeller kit 500104.",
            "estimated_parts_price": "45.00",
            "estimated_completion_date": "2026-08-10",
        }

    def test_requires_a_work_order(self):
        response = self.client.post(self.url, self.data)
        self.assertRedirects(
            response, reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertEqual(Diagnostic.objects.count(), 0)

    def test_records_complete_diagnostic_and_logs(self):
        WorkOrder.objects.create(case=self.case)
        self.client.post(self.url, self.data)
        diagnostic = Diagnostic.objects.get()
        self.assertEqual(diagnostic.issue, "Impeller worn out.")
        self.assertEqual(diagnostic.parts_needed, "Impeller kit 500104.")
        self.assertEqual(diagnostic.estimated_parts_price, Decimal("45.00"))
        self.assertEqual(str(diagnostic.estimated_completion_date), "2026-08-10")
        self.assertTrue(Diagnostic.complete_for_case(self.case).exists())
        log = ActivityLog.objects.filter(case=self.case).latest("created_at", "pk")
        self.assertIn("SVC-09", log.text)

    def test_missing_field_is_rejected(self):
        WorkOrder.objects.create(case=self.case)
        self.client.post(self.url, {**self.data, "parts_needed": ""})
        self.assertEqual(Diagnostic.objects.count(), 0)


class CaseCreateViewTests(TestCase):
    """Staff intake form: creation, initial status, dedupe routing, NTF-04."""

    def setUp(self):
        self.statuses = build_service_workflow()
        self.staffer = get_user_model().objects.create_user(
            username="frontdesk", password="demo12345",
            is_staff=True, role="FRONT_DESK",
        )
        self.client.force_login(self.staffer)
        self.url = reverse("backoffice:case-create")
        self.valid_data = {
            "case_type": CaseType.SERVICE,
            "report_channel": ReportChannel.PHONE,
            "contact_name": "New Client",
            "phone": "+44 67 111 222",
            "symptoms_description": "Engine will not start.",
            # §7.3: a service report says where the asset is.
            "location": "Customer site",
        }

    def test_anonymous_request_redirects_to_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_renders_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "symptoms_description")

    def test_post_creates_case_with_initial_status_and_redirects_to_detail(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self.valid_data)
        case = Case.objects.get()
        self.assertEqual(case.status, self.statuses["new_report"])
        self.assertEqual(case.report_channel, ReportChannel.PHONE)
        self.assertEqual(case.raw_contact_name, "New Client")
        self.assertRedirects(
            response, reverse("backoffice:case-detail", args=[case.pk])
        )
        # Creation is audited; phone-only report also gets the NTF-04
        # staff reminder (no e-mail on file, no SMS gateway).
        texts = list(
            ActivityLog.objects.filter(case=case).values_list("text", flat=True)
        )
        self.assertTrue(any("created in backoffice" in t for t in texts))
        self.assertTrue(any("phone/SMS" in t for t in texts))

    def test_post_with_email_sends_confirmation(self):
        from django.core import mail

        data = {
            **self.valid_data,
            "report_channel": ReportChannel.EMAIL,
            "email": "novi@example.com",
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, data)
        case = Case.objects.get()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["novi@example.com"])
        # NTF-04 confirmation: received, no link - there is no client page.
        self.assertIn("Thank you", mail.outbox[0].body)
        self.assertNotIn(case.public_token, mail.outbox[0].body)

    def test_post_matching_existing_client_redirects_to_triage(self):
        from apps.clients.models import Client as ClientRecord

        ClientRecord.objects.create(
            name="Returning Client", phone="+44 67 111 222"
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self.valid_data)
        case = Case.objects.get()
        self.assertRedirects(
            response, reverse("backoffice:case-triage", args=[case.pk])
        )

    def test_post_transport_case_starts_as_inquiry(self):
        inquiry = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="inquiry",
            name="Upit",
            client_visible_name="Received",
            sort_order=0,
        )
        data = {**self.valid_data, "case_type": CaseType.TRANSPORT}
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, data)
        case = Case.objects.get()
        self.assertEqual(case.status, inquiry)

    def test_email_channel_requires_email(self):
        data = {**self.valid_data, "report_channel": ReportChannel.EMAIL}
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Case.objects.count(), 0)
        self.assertFormError(
            response.context["form"], "email",
            "Email is required for an email report.",
        )

    def test_contact_channel_missing_both_rejected(self):
        data = {**self.valid_data, "phone": ""}
        response = self.client.post(self.url, data)
        self.assertEqual(Case.objects.count(), 0)
        self.assertEqual(response.status_code, 200)


class CaseDetailViewTestBase(TestCase):
    """Shared fixture: a SERVICE workflow, a case and staff users."""

    def setUp(self):
        self.statuses = build_service_workflow()
        # Restrict awaiting_approval -> in_progress to managerial roles
        # (mirrors the seed command, STM-06).
        restricted = StatusTransition.objects.get(
            from_status=self.statuses["awaiting_approval"],
            to_status=self.statuses["in_progress"],
        )
        restricted.allowed_roles = ["MANAGER", "DIRECTOR"]
        restricted.save()

        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.statuses["awaiting_approval"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Test Contact",
            location="Customer site",  # §7.3
        )
        User = get_user_model()
        self.technician = User.objects.create_user(
            username="technician", password="demo12345",
            is_staff=True, role="TECHNICIAN",
        )
        self.manager = User.objects.create_user(
            username="manager", password="demo12345",
            is_staff=True, role="MANAGER",
        )

    def _detail_url(self):
        return reverse("backoffice:case-detail", args=[self.case.pk])


class CaseDetailViewTests(CaseDetailViewTestBase):
    def test_anonymous_request_redirects_to_login(self):
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 302)

    def test_detail_shows_case_and_raw_fields(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Contact")
        self.assertContains(response, self.case.status.name)

    # STM-06: the UI offers only transitions available_transitions()
    # returns for the current user.
    def test_detail_shows_only_permitted_transition_buttons(self):
        self.client.force_login(self.technician)
        response = self.client.get(self._detail_url())
        self.assertNotContains(
            response, 'name="to_status" value="in_progress"'
        )
        self.assertContains(
            response, 'name="to_status" value="awaiting_part"'
        )

        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(
            response, 'name="to_status" value="in_progress"'
        )


class CaseTransitionViewTests(CaseDetailViewTestBase):
    # STM-04: a failed guard surfaces its message naming the missing
    # precondition, and the status must not change.
    def test_failed_guard_renders_message_and_keeps_status(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:case-transition", args=[self.case.pk]),
            {"to_status": "in_progress", "note": ""},
            follow=True,
        )
        # No quote exists, so the SVC-13 guard refuses the transition.
        self.assertContains(response, "no quote")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["awaiting_approval"])
        self.assertEqual(StatusHistory.objects.count(), 0)

    # STM-06: the service refuses a disallowed role even on a forged POST.
    def test_disallowed_role_gets_error_message(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("backoffice:case-transition", args=[self.case.pk]),
            {"to_status": "in_progress", "note": ""},
            follow=True,
        )
        self.assertContains(response, "not allowed")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["awaiting_approval"])

    def test_valid_transition_moves_case_and_records_note(self):
        self.client.force_login(self.technician)
        # in_progress -> awaiting_approval is unrestricted and, coming from
        # in_progress, runs no guard (fixture setup may set status directly).
        self.case.status = self.statuses["in_progress"]
        self.case.save()
        response = self.client.post(
            reverse("backoffice:case-transition", args=[self.case.pk]),
            {"to_status": "awaiting_approval", "note": "back for approval"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status, self.statuses["awaiting_approval"])
        history = StatusHistory.objects.get()
        self.assertEqual(history.note, "back for approval")
        self.assertEqual(history.changed_by, self.technician)


class CaseActivityLogViewTests(CaseDetailViewTestBase):
    # GEN-02: phone agreements are documented through the unified record.
    def test_activity_log_form_creates_entry(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("backoffice:case-activity-log", args=[self.case.pk]),
            {
                "kind": ActivityLog.Kind.PHONE_AGREEMENT,
                "text": "Client agreed by phone to extend the deadline.",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.PHONE_AGREEMENT)
        self.assertEqual(entry.author, self.technician)
        self.assertIn("extend the deadline", entry.text)
        self.assertContains(response, "extend the deadline")

    def test_invalid_form_creates_no_entry(self):
        self.client.force_login(self.technician)
        self.client.post(
            reverse("backoffice:case-activity-log", args=[self.case.pk]),
            {"kind": ActivityLog.Kind.NOTE, "text": ""},
            follow=True,
        )
        self.assertFalse(ActivityLog.objects.filter(case=self.case).exists())

    def test_system_kind_is_not_accepted_from_the_form(self):
        self.client.force_login(self.technician)
        self.client.post(
            reverse("backoffice:case-activity-log", args=[self.case.pk]),
            {"kind": ActivityLog.Kind.SYSTEM, "text": "forged"},
            follow=True,
        )
        self.assertFalse(ActivityLog.objects.filter(case=self.case).exists())


class QuoteActionViewTests(CaseDetailViewTestBase):
    """Integration wiring: quote services driven from the case detail page."""

    def setUp(self):
        super().setUp()
        self.quote = Quote.objects.create(
            case=self.case, description="Impeller replacement", amount=Decimal("100")
        )

    def test_send_quote_moves_draft_to_sent(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:quote-send", args=[self.case.pk, self.quote.pk]),
            follow=True,
        )
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)
        self.assertContains(response, "sent")

    def test_send_quote_twice_surfaces_validation_error(self):
        self.client.force_login(self.manager)
        url = reverse("backoffice:quote-send", args=[self.case.pk, self.quote.pk])
        self.client.post(url)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "only DRAFT quotes can be sent")

    def test_record_client_approval_accepts_quote(self):
        # No ApprovalLimit rows -> conservative ladder needs DIRECTOR too,
        # so record the internal approval first, then client consent.
        self.quote.status = Quote.Status.SENT
        self.quote.save()
        director = get_user_model().objects.create_user(
            username="director", password="demo12345", is_staff=True, role="DIRECTOR"
        )
        self.client.force_login(director)
        url = reverse(
            "backoffice:quote-approval", args=[self.case.pk, self.quote.pk]
        )
        self.client.post(
            url, {"level": Approval.Level.DIRECTOR, "decision": "approved"}
        )
        response = self.client.post(
            url,
            {"level": Approval.Level.CLIENT, "decision": "approved", "note": "by phone"},
            follow=True,
        )
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        self.assertContains(response, "Accepted")

    def test_underqualified_role_gets_error_message(self):
        self.quote.status = Quote.Status.SENT
        self.quote.save()
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse("backoffice:quote-approval", args=[self.case.pk, self.quote.pk]),
            {"level": Approval.Level.DIRECTOR, "decision": "approved"},
            follow=True,
        )
        self.assertContains(response, "requires role")
        self.assertFalse(self.quote.approvals.exists())

    def test_request_advance_creates_payment(self):
        self.quote.status = Quote.Status.SENT
        self.quote.save()
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:quote-advance", args=[self.case.pk, self.quote.pk]),
            {"percent": "50"},
            follow=True,
        )
        advance = self.case.advance_payments.get()
        self.assertEqual(advance.amount, Decimal("50.00"))
        self.assertEqual(advance.quote, self.quote)
        self.assertContains(response, "Advance payment")

    def test_quote_from_another_case_is_404(self):
        other = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.statuses["new_report"],
            report_channel=ReportChannel.PHONE,
        )
        other_quote = Quote.objects.create(
            case=other, description="x", amount=Decimal("10")
        )
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:quote-send", args=[self.case.pk, other_quote.pk])
        )
        self.assertEqual(response.status_code, 404)


class PartOrderReceiveViewTests(CaseDetailViewTestBase):
    def setUp(self):
        super().setUp()
        self.part = Part.objects.create(
            code="IMP-01", name="Impeller", is_consumable=True, stock_qty=3
        )
        self.order = PartOrder.objects.create(case=self.case, part=self.part)

    def test_receive_button_marks_order_received_and_bumps_stock(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            reverse(
                "backoffice:part-order-receive", args=[self.case.pk, self.order.pk]
            ),
            follow=True,
        )
        self.order.refresh_from_db()
        self.part.refresh_from_db()
        self.assertIsNotNone(self.order.received_at)
        self.assertEqual(self.part.stock_qty, 4)
        self.assertContains(response, "marked received")

    def test_receiving_twice_surfaces_error(self):
        self.client.force_login(self.technician)
        url = reverse(
            "backoffice:part-order-receive", args=[self.case.pk, self.order.pk]
        )
        self.client.post(url)
        response = self.client.post(url, follow=True)
        self.assertContains(response, "already received")


class CaseDetailIntegrationRenderingTests(CaseDetailViewTestBase):
    """Triage banner (GEN-03) and handover comparison (SVC-18) rendering."""

    def test_triage_banner_shown_for_unlinked_case_with_raw_data(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(
            response, reverse("backoffice:case-triage", args=[self.case.pk])
        )

    def test_no_triage_banner_once_client_is_linked(self):
        from apps.clients.models import Client

        self.case.client = Client.objects.create(name="Linked Client")
        self.case.save()
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertNotContains(
            response, reverse("backoffice:case-triage", args=[self.case.pk])
        )

    def test_handover_comparison_rendered_in_ready_for_handover(self):
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()
        record = IntakeRecord.objects.create(
            case=self.case,
            received_by=self.manager,
            mount_status="OFF_MOUNT",
            signed_by=self.manager,
            signed_at=timezone.now(),
        )
        IntakeItem.objects.create(
            record=record, item_name="Anchor", quantity=1, condition_note="scratched"
        )
        HandoverRecord.objects.create(
            case=self.case, discrepancies_note="Fender missing"
        )
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Handover vs. intake comparison")
        self.assertContains(response, "Anchor")
        self.assertContains(response, "Fender missing")

    def test_no_comparison_card_outside_ready_for_handover(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertNotContains(response, "Handover vs. intake comparison")


class HandoverViewTests(CaseDetailViewTestBase):
    """SVC-18: create / note / sign the handover record from the detail page."""

    def setUp(self):
        super().setUp()
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save(update_fields=["status"])
        # SVC-18: the handover record is drawn up once the work is
        # finished. These tests jump straight to ready_for_handover
        # instead of walking the transitions, so the signed work order
        # that every real case at this status carries has to be stated.
        WorkOrder.objects.create(
            case=self.case, is_preliminary=False, signed_at=timezone.now()
        )

    def _create_url(self):
        return reverse("backoffice:handover-create", args=[self.case.pk])

    def test_detail_renders_create_form_when_no_record(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Create handover record")

    def test_create_note_sign_happy_path(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            self._create_url(),
            {"discrepancies_note": "Fender missing."},
            follow=True,
        )
        self.assertContains(response, "created")
        handover = HandoverRecord.objects.get(case=self.case)
        self.assertEqual(handover.discrepancies_note, "Fender missing.")

        # Unsigned record renders the note + sign forms.
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Save note")
        self.assertContains(response, "Sign handover")

        response = self.client.post(
            reverse(
                "backoffice:handover-note", args=[self.case.pk, handover.pk]
            ),
            {"discrepancies_note": "Fender missing; scratch on bow."},
            follow=True,
        )
        self.assertContains(response, "saved")
        handover.refresh_from_db()
        self.assertEqual(
            handover.discrepancies_note, "Fender missing; scratch on bow."
        )

        from django.core.files.uploadedfile import SimpleUploadedFile

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)

        # §7/SVC-07: refused first of all until the asset has been
        # photographed on the way out - that happens before anyone signs
        # anything.
        response = self.client.post(
            reverse(
                "backoffice:handover-sign", args=[self.case.pk, handover.pk]
            ),
            {"received_by_name": "Jane Smith"},
            follow=True,
        )
        self.assertContains(response, "Photograph the asset as it is handed over")

        with override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse(
                    "backoffice:handover-photos",
                    args=[self.case.pk, handover.pk],
                ),
                {"photos": SimpleUploadedFile("handover.jpg", b"fake-jpeg")},
                follow=True,
            )
        self.assertContains(response, "Handover documentation uploaded")

        # SVC-18: and still refused until the worker signs in-app and the
        # client-signed scan is on file.
        response = self.client.post(
            reverse(
                "backoffice:handover-sign", args=[self.case.pk, handover.pk]
            ),
            {"received_by_name": "Jane Smith"},
            follow=True,
        )
        self.assertContains(response, "not signed by the responsible worker")

        response = self.client.post(
            reverse(
                "backoffice:handover-tech-sign",
                args=[self.case.pk, handover.pk],
            ),
            follow=True,
        )
        self.assertContains(response, "signed by the worker")
        handover.refresh_from_db()
        self.assertIsNotNone(handover.technician_signed_at)

        with override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse(
                    "backoffice:handover-approval",
                    args=[self.case.pk, handover.pk],
                ),
                {"file": SimpleUploadedFile("handover.pdf", b"%PDF-1.4 scan")},
                follow=True,
            )
            self.assertContains(response, "Client handover approval uploaded")

            response = self.client.post(
                reverse(
                    "backoffice:handover-sign", args=[self.case.pk, handover.pk]
                ),
                {"received_by_name": "Jane Smith"},
                follow=True,
            )
        self.assertContains(response, "signed")
        handover.refresh_from_db()
        self.assertEqual(handover.received_by_name, "Jane Smith")
        self.assertIsNotNone(handover.signed_at)

        # Signed record renders the signature line instead of the forms.
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Received by")
        self.assertNotContains(response, "Sign handover")

    def test_sign_requires_a_name(self):
        self.client.force_login(self.manager)
        handover = HandoverRecord.objects.create(case=self.case)
        response = self.client.post(
            reverse(
                "backoffice:handover-sign", args=[self.case.pk, handover.pk]
            ),
            {"received_by_name": "   "},
            follow=True,
        )
        # The refusal is now a field error on the input that caused it,
        # not a flash message quoting the rule tag.
        self.assertContains(response, "invalid-feedback")
        self.assertContains(response, "This field is required.")
        handover.refresh_from_db()
        self.assertIsNone(handover.signed_at)

    def test_second_unsigned_record_surfaces_error_message(self):
        self.client.force_login(self.manager)
        HandoverRecord.objects.create(case=self.case)
        response = self.client.post(self._create_url(), {}, follow=True)
        self.assertContains(response, "already has an unsigned handover record")
        self.assertEqual(HandoverRecord.objects.count(), 1)

    def test_comparison_card_renders_for_transport_delivered(self):
        delivered = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="delivered",
            name="Delivered",
            client_visible_name="Delivered",
        )
        transport_case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=delivered,
        )
        self.client.force_login(self.manager)
        response = self.client.get(
            reverse("backoffice:case-detail", args=[transport_case.pk])
        )
        self.assertContains(response, "Handover vs. intake comparison")
        self.assertContains(response, "Closing checklist")


class FinancialSettlementViewTests(CaseDetailViewTestBase):
    """SVC-19: the settlement toggle is manager-only and audited."""

    def setUp(self):
        super().setUp()
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save(update_fields=["status"])

    def _settlement_url(self):
        return reverse("backoffice:case-settlement", args=[self.case.pk])

    def test_manager_settles_case(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            self._settlement_url(), {"settled": "1"}, follow=True
        )
        self.assertContains(response, "financially settled")
        self.case.refresh_from_db()
        self.assertTrue(self.case.financially_settled)
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)

    def test_technician_is_refused(self):
        self.client.force_login(self.technician)
        response = self.client.post(
            self._settlement_url(), {"settled": "1"}, follow=True
        )
        self.assertContains(response, "may not change financial settlement")
        self.case.refresh_from_db()
        self.assertFalse(self.case.financially_settled)

    def test_settle_toggle_hidden_from_technician(self):
        self.client.force_login(self.technician)
        response = self.client.get(self._detail_url())
        self.assertNotContains(response, "Mark financially settled")
        self.assertContains(response, "set by a manager or director")

        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Mark financially settled")


class BackofficeActionViewTests(CaseDetailViewTestBase):
    """Thin action views over the new domain services (STM-06 spirit:
    the UI delegates, the services enforce)."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.manager)

    def _post(self, url_name, data=None, args=None):
        return self.client.post(
            reverse("backoffice:%s" % url_name, args=args or [self.case.pk]),
            data or {},
            follow=True,
        )

    def test_book_appointment_happy_and_invalid(self):
        from apps.scheduling.models import Appointment

        response = self._post(
            "appointment-book",
            {
                "kind": "INTAKE",
                "scheduled_for": "2027-08-04T10:00",
                "note": "Morning",
            },
        )
        self.assertContains(response, "booked")
        self.assertEqual(Appointment.objects.count(), 1)

        response = self._post(
            "appointment-book",
            {"kind": "INTAKE", "scheduled_for": "2001-01-01T10:00"},
        )
        self.assertContains(response, "in the past")
        self.assertEqual(Appointment.objects.count(), 1)

    def test_work_location_consult_recorded_from_the_case_page(self):
        """SVC-03: the consult is answered on the case page and lands in
        the activity log, not just in a field."""
        from apps.cases.models import ActivityLog, WorkLocation

        response = self._post(
            "case-work-location",
            {"work_location": WorkLocation.ON_SITE, "note": "Stays on site"},
        )
        self.assertContains(response, "At the client&#x27;s site")
        self.case.refresh_from_db()
        self.assertEqual(self.case.work_location, WorkLocation.ON_SITE)
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, kind=ActivityLog.Kind.NOTE, text__contains="SVC-03"
            ).exists()
        )

    def test_work_location_rejects_an_empty_answer(self):
        response = self._post("case-work-location", {"work_location": ""})
        self.assertContains(response, "correct the highlighted fields")
        self.case.refresh_from_db()
        self.assertEqual(self.case.work_location, "")

    def test_on_site_case_is_not_offered_the_intake_form(self):
        """The card explains instead of offering a form that would only
        invite a false mount status."""
        from apps.cases.services import record_work_location_consult

        record_work_location_consult(
            self.case, "ON_SITE", self.manager
        )
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertNotContains(response, "Create intake record")
        self.assertContains(response, "no intake record is written")

    def test_work_order_create_finalize_approve_sign_flow(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.service.models import WorkOrder

        response = self._post("work-order-create")
        self.assertContains(response, "opened")
        work_order = WorkOrder.objects.get(case=self.case)

        response = self._post("work-order-finalize")
        self.assertContains(response, "final")
        work_order.refresh_from_db()
        self.assertFalse(work_order.is_preliminary)

        # SVC-05: signing needs the client-approval scan first, and only
        # a technician may sign.
        response = self._post("work-order-sign")
        self.assertContains(response, "Only a technician")

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            self.client.force_login(self.technician)
            response = self._post("work-order-sign")
            self.assertContains(response, "no client-approval scan")

            response = self._post(
                "work-order-approval",
                {"file": SimpleUploadedFile("work-order.pdf", b"%PDF-1.4 scan")},
            )
            self.assertContains(response, "Client approval uploaded")

            response = self._post("work-order-sign")
        self.assertContains(response, "signed")
        work_order.refresh_from_db()
        self.assertIsNotNone(work_order.signed_at)
        self.assertEqual(work_order.signed_by_technician, self.technician)
        self.case.refresh_from_db()
        self.assertEqual(self.case.assigned_technician, self.technician)

        # Duplicate create surfaces the service refusal via messages.
        self.client.force_login(self.manager)
        response = self._post("work-order-create")
        self.assertContains(response, "already has work order")

    def test_work_order_actions_without_order_surface_error(self):
        response = self._post("work-order-finalize")
        self.assertContains(response, "no work order")

    def test_intake_create_and_sign(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.models import Attachment

        # SVC-02: on a SERVICE case the work order comes first, so the
        # intake record is refused until one is open (preliminary is enough).
        WorkOrder.objects.create(case=self.case)

        # SVC-06: the mount status is mandatory - the form refuses a
        # record without it.
        response = self._post(
            "intake-create",
            {"storage_location": "Hall 1", "general_condition_note": "OK"},
        )
        self.assertContains(response, "mount_status")
        self.assertFalse(IntakeRecord.objects.filter(case=self.case).exists())

        # SVC-07: arrival photos are mandatory on the create form.
        response = self._post(
            "intake-create",
            {
                "mount_status": "ON_MOUNT",
                "cleanliness_status": "CLEAN",
                "storage_location": "Hall 1",
                "damage_note": "Scratch on port side.",
                "energy_level": "HALF",  # §7.6
                "general_condition_note": "OK",
            },
        )
        self.assertContains(response, "photos")
        self.assertFalse(IntakeRecord.objects.filter(case=self.case).exists())

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            response = self._post(
                "intake-create",
                {
                    "mount_status": "ON_MOUNT",
                    "cleanliness_status": "CLEAN",
                    "storage_location": "Hall 1",
                    "damage_note": "Scratch on port side.",
                    "energy_level": "HALF",  # §7.6
                    "general_condition_note": "OK",
                    "photos": [
                        SimpleUploadedFile("bow.jpg", b"fake-jpeg"),
                        SimpleUploadedFile("stern.png", b"fake-png"),
                    ],
                    "videos": SimpleUploadedFile("walkaround.mp4", b"fake-video"),
                },
            )
        self.assertContains(response, "created")
        record = IntakeRecord.objects.get(case=self.case)
        self.assertEqual(record.storage_location, "Hall 1")
        self.assertEqual(record.mount_status, "ON_MOUNT")
        self.assertEqual(record.damage_note, "Scratch on port side.")
        intake_files = Attachment.objects.filter(stage=Attachment.Stage.INTAKE)
        self.assertEqual(
            intake_files.filter(kind=Attachment.Kind.PHOTO).count(), 2
        )
        self.assertEqual(
            intake_files.filter(kind=Attachment.Kind.VIDEO).count(), 1
        )

        # SVC-06: the inventory is answered before receipt is confirmed.
        self._post("intake-no-items", args=[self.case.pk, record.pk])

        # §7.2: and the receipt has to say which asset it is for.
        response = self._post("intake-sign", args=[self.case.pk, record.pk])
        self.assertContains(response, "no asset record yet")
        record.refresh_from_db()
        self.assertIsNone(record.signed_at)
        link_identified_asset(self.case)

        response = self._post(
            "intake-sign", args=[self.case.pk, record.pk]
        )
        self.assertContains(response, "signed")
        record.refresh_from_db()
        self.assertIsNotNone(record.signed_at)
        # No technician assigned - falls back to the acting manager.
        self.assertEqual(record.signed_by, self.manager)

    def test_amendment_is_appended_to_a_signed_intake_record(self):
        """SVC-07 / INV-12: the append path had no caller in the whole
        application before this - it was reachable only from the admin."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.intake.models import IntakeAmendment, IntakeRecord
        from apps.service.models import WorkOrder

        WorkOrder.objects.create(case=self.case)
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            self._post(
                "intake-create",
                {
                    "mount_status": "ON_MOUNT",
                    "cleanliness_status": "CLEANED",
                    "damage_note": "none",
                    "energy_level": "HALF",  # §7.6
                    "photos": SimpleUploadedFile("bow.jpg", b"fake-jpeg"),
                },
            )
            record = IntakeRecord.objects.get(case=self.case)
            self._post("intake-no-items", args=[self.case.pk, record.pk])
            self._post("intake-sign", args=[self.case.pk, record.pk])

            response = self._post(
                "intake-amendment-add",
                {"text": "Gelcoat crack found under the bow."},
                args=[self.case.pk, record.pk],
            )
        self.assertContains(response, "Amendment")
        amendment = IntakeAmendment.objects.get(record=record)
        self.assertEqual(amendment.text, "Gelcoat crack found under the bow.")
        self.assertEqual(amendment.author, self.manager)
        # The signed record itself is untouched - that is the whole rule.
        record.refresh_from_db()
        self.assertEqual(record.damage_note, "none")
        self.assertContains(response, "Gelcoat crack")

    def test_empty_amendment_is_rejected_on_the_field(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.intake.models import IntakeRecord
        from apps.service.models import WorkOrder

        WorkOrder.objects.create(case=self.case)
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            self._post(
                "intake-create",
                {
                    "mount_status": "ON_MOUNT",
                    "cleanliness_status": "CLEAN",
                    "damage_note": "none",
                    "energy_level": "HALF",  # §7.6
                    "photos": SimpleUploadedFile("bow.jpg", b"fake-jpeg"),
                },
            )
            record = IntakeRecord.objects.get(case=self.case)
            response = self._post(
                "intake-amendment-add",
                {"text": "   "},
                args=[self.case.pk, record.pk],
            )
        self.assertContains(response, "correct the highlighted fields")
        self.assertEqual(record.amendments.count(), 0)

    def test_storage_location_is_demanded_off_the_mount(self):
        """SVC-06: the asset comes off its mount, so where it goes is
        recorded - and the error lands on the field, not in a flash."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.intake.models import IntakeRecord
        from apps.service.models import WorkOrder

        WorkOrder.objects.create(case=self.case)
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            response = self._post(
                "intake-create",
                {
                    "mount_status": "OFF_MOUNT",
                    "cleanliness_status": "CLEAN",
                    "damage_note": "none",
                    "energy_level": "HALF",  # §7.6
                    "photos": SimpleUploadedFile("bow.jpg", b"fake-jpeg"),
                },
            )
        self.assertContains(response, "where the asset will be stored")
        self.assertFalse(IntakeRecord.objects.filter(case=self.case).exists())

    def test_quote_create_happy_and_invalid(self):
        response = self._post(
            "quote-create",
            {"description": "Impeller", "amount": "850.00"},
        )
        self.assertContains(response, "created")
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(str(quote.amount), "850.00")

        self._post("quote-create", {"description": "x", "amount": "-5"})
        self.assertEqual(Quote.objects.count(), 1)

    def test_advance_mark_paid(self):
        from decimal import Decimal as D

        from apps.quotes.models import AdvancePayment

        advance = AdvancePayment.objects.create(
            case=self.case, percent=50, amount=D("100.00")
        )
        response = self._post("advance-paid", args=[self.case.pk, advance.pk])
        self.assertContains(response, "marked paid")
        advance.refresh_from_db()
        self.assertIsNotNone(advance.paid_at)
        # Second click surfaces the refusal.
        response = self._post("advance-paid", args=[self.case.pk, advance.pk])
        self.assertContains(response, "already")

    def test_detail_renders_new_cards_and_activity_form(self):
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Appointments")
        self.assertContains(response, "Work order")
        self.assertContains(response, "Intake records")
        self.assertContains(response, "Create quote")
        self.assertContains(response, "Upload")
        # The activity-log form now actually renders its fields
        # (activity_form was missing from context before).
        self.assertContains(response, 'name="kind"')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AttachmentUploadViewTests(CaseDetailViewTestBase):
    """INV-08: staff upload from the case detail page."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._overridden_settings["MEDIA_ROOT"], ignore_errors=True)
        super().tearDownClass()

    def test_upload_and_list(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.models import Attachment

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:attachment-upload", args=[self.case.pk]),
            {
                "file": SimpleUploadedFile("Engine Photo.jpg", b"fake"),
                "kind": "photo",
                "stage": "work",
            },
            follow=True,
        )
        self.assertContains(response, "uploaded")
        attachment = Attachment.objects.get()
        self.assertEqual(attachment.original_name, "Engine Photo.jpg")
        self.assertEqual(attachment.uploaded_by, self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Engine Photo.jpg")

    def test_bad_extension_surfaces_error(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.models import Attachment

        self.client.force_login(self.manager)
        self.client.post(
            reverse("backoffice:attachment-upload", args=[self.case.pk]),
            {
                "file": SimpleUploadedFile("evil.exe", b"MZ"),
                "kind": "document",
                "stage": "work",
            },
            follow=True,
        )
        self.assertEqual(Attachment.objects.count(), 0)


class TransportCardViewTests(TestCase):
    """TRP-08: the delivery card renders and confirming the protocol
    stamps the delivery."""

    def setUp(self):
        from apps.transport.models import TransportOrder

        User = get_user_model()
        self.manager = User.objects.create_user(
            username="manager3", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        in_transit = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="in_transit",
            name="In transit",
            client_visible_name="In transit",
        )
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=in_transit,
        )
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        self.client.force_login(self.manager)

    def test_card_renders_and_confirms_delivery(self):
        url = reverse("backoffice:case-detail", args=[self.case.pk])
        response = self.client.get(url)
        # The heading names the step, not the rule that mandates it.
        self.assertContains(response, "Confirm delivery (sign)")
        self.assertNotContains(response, "TRP-08")
        self.assertContains(response, "Confirm delivery (sign)")

        response = self.client.post(
            reverse("backoffice:transport-delivery", args=[self.case.pk]),
            {
                "unloading_done": "on",
                "put_into_service": "on",
                "attendee_role": "OWNER",
                "attendee_name": "Pat Owner",
                "attendee_contact": "067 123 456",
                "notes": "Bay 3",
            },
            follow=True,
        )
        self.assertContains(response, "Delivery confirmed")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)
        confirmation = self.order.delivery_confirmation
        self.assertEqual(confirmation.signed_by, self.manager)
        self.assertTrue(confirmation.unloading_done)
        self.assertTrue(confirmation.put_into_service)

    def test_blank_attendee_surfaces_form_error(self):
        response = self.client.post(
            reverse("backoffice:transport-delivery", args=[self.case.pk]),
            {"attendee_role": "OWNER", "attendee_name": ""},
            follow=True,
        )
        self.assertContains(response, "field is required")
        from apps.transport.models import DeliveryConfirmation

        self.assertEqual(DeliveryConfirmation.objects.count(), 0)

    def test_protocol_upload_attaches_scan(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.attachments.models import Attachment
        from apps.transport.services import record_delivery_confirmation

        # Without a confirmation the upload is refused with a hint.
        media_root = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=media_root):
            response = self.client.post(
                reverse(
                    "backoffice:transport-delivery-protocol", args=[self.case.pk]
                ),
                {"file": SimpleUploadedFile("record.pdf", b"%PDF-1.4 scan")},
                follow=True,
            )
            self.assertContains(response, "no delivery confirmation")
            self.assertEqual(Attachment.objects.count(), 0)

            record_delivery_confirmation(
                self.case,
                user=self.manager,
                attendee_name="Pat Owner",
                unloading_done=True,
                put_into_service=True,
            )
            response = self.client.post(
                reverse(
                    "backoffice:transport-delivery-protocol", args=[self.case.pk]
                ),
                {"file": SimpleUploadedFile("record.pdf", b"%PDF-1.4 scan")},
                follow=True,
            )
            self.assertContains(response, "record.pdf")
            attachment = Attachment.objects.get()
            self.assertEqual(attachment.kind, Attachment.Kind.DOCUMENT)
            self.assertEqual(attachment.stage, Attachment.Stage.HANDOVER)
        shutil.rmtree(media_root, ignore_errors=True)


class TransportCarrierPriceViewTests(TestCase):
    """TRP-03: an asset over ~10 m shows the carrier-price form; declaring
    the found carrier's price records it and hides the form."""

    def setUp(self):
        from apps.clients.models import Client, Asset
        from apps.transport.models import TransportOrder

        User = get_user_model()
        self.manager = User.objects.create_user(
            username="manager_cp", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        status = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="inquiry",
            name="Inquiry",
            client_visible_name="Inquiry",
        )
        client = Client.objects.create(name="John Smith")
        asset = Asset.objects.create(
            client=client, name="Big Asset", asset_type="Machine",
            length_m=Decimal("12.00"),
        )
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=status,
            client=client,
            asset=asset,
        )
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        self.client.force_login(self.manager)

    def test_form_shows_and_declaring_records_price(self):
        url = reverse("backoffice:case-detail", args=[self.case.pk])
        self.assertContains(self.client.get(url), "Declare price")

        self.client.post(
            reverse("backoffice:transport-carrier-price", args=[self.case.pk]),
            {"carrier_name": "Carrier Ltd", "carrier_price": "4200,00"},
            follow=True,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.carrier_price, Decimal("4200.00"))
        self.assertIsNotNone(self.order.carrier_price_declared_at)

        # The form is replaced by the recorded price.
        response = self.client.get(url)
        self.assertNotContains(response, "Declare price")
        self.assertContains(response, "Carrier Ltd")


class CaseListStatusFilterTests(TestCase):
    """The status filter targets an exact Status row: workflows of
    different case types reuse names/codes ("Upit"/"inquiry" exists for
    TRANSPORT and STORAGE both), so filtering by code was
    ambiguous and the flat dropdown showed duplicate labels."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="lister", password="demo12345", is_staff=True
        )
        self.transport_inquiry = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="inquiry",
            name="Upit",
            client_visible_name="Received",
        )
        self.storage_inquiry = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="inquiry",
            name="Upit",
            client_visible_name="Received",
        )
        self.transport_case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.transport_inquiry,
            raw_contact_name="Transport Client",
        )
        self.storage_case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.storage_inquiry,
            raw_contact_name="Storage Client",
        )
        self.client.force_login(self.user)

    def test_filter_by_status_pk_separates_same_named_statuses(self):
        response = self.client.get(
            reverse("backoffice:case-list"),
            {"status": self.transport_inquiry.pk},
        )
        self.assertContains(response, "Transport Client")
        self.assertNotContains(response, "Storage Client")

        response = self.client.get(
            reverse("backoffice:case-list"),
            {"status": self.storage_inquiry.pk},
        )
        self.assertContains(response, "Storage Client")
        self.assertNotContains(response, "Transport Client")

    def test_dropdown_groups_statuses_by_case_type(self):
        response = self.client.get(reverse("backoffice:case-list"))
        self.assertContains(response, "<optgroup")
        self.assertContains(response, 'value="%s"' % self.transport_inquiry.pk)
        self.assertContains(response, 'value="%s"' % self.storage_inquiry.pk)


class HomeViewTests(TestCase):
    """The backoffice landing page: one tile per business line with a
    live open-case count, plus shortcuts to the pages that exist."""

    def _make_statuses(self):
        self.service_open = Status.objects.create(
            case_type=CaseType.SERVICE, code="new_report",
            name="New report", client_visible_name="New report",
        )
        self.service_closed = Status.objects.create(
            case_type=CaseType.SERVICE, code="closed",
            name="Closed", client_visible_name="Closed",
            sort_order=1, is_terminal=True,
        )
        self.transport_open = Status.objects.create(
            case_type=CaseType.TRANSPORT, code="inquiry",
            name="Inquiry", client_visible_name="Inquiry",
        )

    def test_anonymous_request_redirects_to_login(self):
        from django.conf import settings

        response = self.client.get(reverse("backoffice:home"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)

    def test_non_staff_user_gets_403(self):
        user = get_user_model().objects.create_user(
            username="visitor", password="demo12345", is_staff=False
        )
        self.client.force_login(user)
        response = self.client.get(reverse("backoffice:home"))
        self.assertEqual(response.status_code, 403)

    def test_staff_sees_section_tiles_and_shortcuts(self):
        user = get_user_model().objects.create_user(
            username="homer", password="demo12345", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.get(reverse("backoffice:home"))
        self.assertEqual(response.status_code, 200)
        for url_name in (
            "backoffice:section-service",
            "backoffice:section-storage",
            "backoffice:section-transport",
            "backoffice:case-create",
            "backoffice:case-list",
            "admin:index",
        ):
            self.assertContains(response, 'href="%s"' % reverse(url_name))

    def test_open_counts_exclude_terminal_cases(self):
        self._make_statuses()
        Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.service_open,
        )
        Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.service_closed,
        )
        Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.transport_open,
        )
        user = get_user_model().objects.create_user(
            username="counter", password="demo12345", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.get(reverse("backoffice:home"))
        counts = {
            section["url_name"]: section["open_count"]
            for section in response.context["sections"]
        }
        self.assertEqual(counts["backoffice:section-service"], 1)
        self.assertEqual(counts["backoffice:section-storage"], 0)
        self.assertEqual(counts["backoffice:section-transport"], 1)

    def test_landing_lists_recent_open_works_and_untriaged_reports(self):
        """The space under the tiles carries the open works touched most
        recently and the reports still waiting for triage; closed works
        and triaged reports stay out of the respective lists."""
        from apps.clients.models import Client as ClientRecord

        self._make_statuses()
        owner = ClientRecord.objects.create(name="Asset Owner", phone="067 1")
        open_linked = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.service_open,
            client=owner,
            raw_contact_name="Linked Reporter",
        )
        untriaged = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.transport_open,
            raw_contact_name="Untriaged Reporter",
        )
        closed = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.service_closed,
            raw_contact_name="Closed Reporter",
        )
        user = get_user_model().objects.create_user(
            username="lister", password="demo12345", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.get(reverse("backoffice:home"))
        self.assertEqual(response.status_code, 200)

        recent = list(response.context["recent_cases"])
        self.assertIn(open_linked, recent)
        self.assertIn(untriaged, recent)
        self.assertNotIn(closed, recent)
        self.assertEqual(list(response.context["untriaged_cases"]), [untriaged])
        self.assertEqual(response.context["untriaged_count"], 1)

        self.assertContains(response, "Asset Owner")
        self.assertContains(response, "Untriaged Reporter")
        self.assertNotContains(response, "Closed Reporter")
        self.assertContains(
            response, 'href="%s"' % reverse("backoffice:case-triage", args=[untriaged.pk])
        )
        self.assertContains(response, 'href="%s"' % reverse("backoffice:client-list"))
        self.assertContains(
            response, 'href="%s"' % reverse("backoffice:client-detail", args=[owner.pk])
        )

        # The new strings are in the sr_Latn catalogue, plural forms included.
        from django.conf import settings

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sr-latn"
        response = self.client.get(reverse("backoffice:home"))
        self.assertContains(response, "Nedavno ažurirani radovi")
        self.assertContains(response, "Čeka trijažu")
        self.assertContains(response, "0 uređaja")


class SectionListViewTests(TestCase):
    """Per-section pages are the case list scoped to one case type:
    the type filter disappears and the status dropdown only offers that
    workflow's statuses."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="sectioner", password="demo12345", is_staff=True
        )
        self.storage_status = Status.objects.create(
            case_type=CaseType.STORAGE, code="inquiry",
            name="Storage inquiry", client_visible_name="Received",
        )
        self.transport_status = Status.objects.create(
            case_type=CaseType.TRANSPORT, code="inquiry",
            name="Transport inquiry", client_visible_name="Received",
        )
        self.storage_case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.storage_status,
            raw_contact_name="Storage Client",
        )
        self.transport_case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel=ReportChannel.PHONE,
            status=self.transport_status,
            raw_contact_name="Transport Client",
        )
        self.client.force_login(self.user)

    def test_storage_lists_only_storage_cases(self):
        response = self.client.get(reverse("backoffice:section-storage"))
        self.assertContains(response, "Storage Client")
        self.assertNotContains(response, "Transport Client")
        self.assertEqual(
            [case.case_type for case in response.context["cases"]],
            [CaseType.STORAGE],
        )

    def test_section_status_dropdown_scoped_to_type(self):
        response = self.client.get(reverse("backoffice:section-storage"))
        self.assertEqual(
            [status.case_type for status in response.context["statuses"]],
            [CaseType.STORAGE],
        )

    def test_section_hides_type_filter(self):
        response = self.client.get(reverse("backoffice:section-transport"))
        self.assertNotContains(response, 'name="case_type"')

    def test_transport_section_shows_transport_columns(self):
        from apps.transport.models import TransportOrder

        TransportOrder.objects.create(
            case=self.transport_case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
            preferred_date="2026-09-15",
        )
        response = self.client.get(reverse("backoffice:section-transport"))
        self.assertContains(response, "15.09.2026")
        self.assertEqual(response.context["section"], CaseType.TRANSPORT)

    def test_section_status_filter_still_works(self):
        other_storage_status = Status.objects.create(
            case_type=CaseType.STORAGE, code="contracted",
            name="Contracted", client_visible_name="Contracted",
            sort_order=1,
        )
        Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=other_storage_status,
            raw_contact_name="Contracted Client",
        )
        response = self.client.get(
            reverse("backoffice:section-storage"),
            {"status": other_storage_status.pk},
        )
        self.assertContains(response, "Contracted Client")
        self.assertNotContains(response, "Storage Client")

    def test_case_list_all_still_unfiltered(self):
        response = self.client.get(reverse("backoffice:case-list"))
        self.assertContains(response, "Storage Client")
        self.assertContains(response, "Transport Client")
        self.assertContains(response, 'name="case_type"')


class LoginRedirectTests(TestCase):
    """After login staff land on the section-picker homepage."""

    def test_login_redirect_setting(self):
        from django.conf import settings

        self.assertEqual(settings.LOGIN_REDIRECT_URL, "/backoffice/")

    def test_login_redirects_to_homepage(self):
        from django.conf import settings

        get_user_model().objects.create_user(
            username="landing", password="demo12345", is_staff=True
        )
        response = self.client.post(
            settings.LOGIN_URL,
            {"username": "landing", "password": "demo12345"},
        )
        self.assertRedirects(response, "/backoffice/")


class StorageCaseCreateTests(TestCase):
    """WNT-01: the create form persists storage documentation on the
    storage agreement; the type preselects from the querystring."""

    def setUp(self):
        from apps.cases.tests import build_workflow

        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "quote_sent"]
        )
        self.staffer = get_user_model().objects.create_user(
            username="zimfront", password="demo12345",
            is_staff=True, role="FRONT_DESK",
        )
        self.client.force_login(self.staffer)
        self.url = reverse("backoffice:case-create")
        self.valid_data = {
            "case_type": CaseType.STORAGE,
            "report_channel": ReportChannel.PHONE,
            "contact_name": "Storage Client",
            "phone": "+44 67 555 111",
            "symptoms_description": "Storage inquiry.",
        }

    def test_get_preselects_type_from_querystring(self):
        response = self.client.get(self.url, {"case_type": "STORAGE"})
        self.assertContains(response, 'value="STORAGE" selected')

    def test_post_with_documentation_creates_agreement(self):
        from apps.storage.models import StorageAgreement

        data = {
            **self.valid_data,
            "permit_no": "MNE-4711",
            "asset_length_m": "8.50",
            "asset_width_m": "3.10",
            "origin_site": "Depot A",
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, data)
        agreement = StorageAgreement.objects.get()
        self.assertEqual(agreement.permit_no, "MNE-4711")
        self.assertEqual(agreement.asset_length_m, Decimal("8.50"))
        self.assertEqual(agreement.asset_width_m, Decimal("3.10"))
        self.assertEqual(agreement.origin_site, "Depot A")

    def test_post_without_documentation_creates_no_agreement(self):
        from apps.storage.models import StorageAgreement

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, self.valid_data)
        self.assertEqual(StorageAgreement.objects.count(), 0)

    def test_service_post_ignores_storage_fields(self):
        from apps.storage.models import StorageAgreement

        build_service_workflow()
        data = {
            **self.valid_data,
            "case_type": CaseType.SERVICE,
            "permit_no": "MNE-9999",
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.url, data)
        self.assertEqual(StorageAgreement.objects.count(), 0)


class StorageDocsViewTests(TestCase):
    """WNT-01: the documentation card upserts the agreement from the
    case page and surfaces the OPEN-05 price deviation."""

    def setUp(self):
        from apps.cases.tests import build_workflow

        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "quote_sent"]
        )
        self.manager = get_user_model().objects.create_user(
            username="zimmgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            raw_contact_name="Storage Client",
            raw_phone="+44 67 555 111",
        )
        self.client.force_login(self.manager)
        self.url = reverse("backoffice:storage-docs", args=[self.case.pk])

    def test_post_creates_agreement_and_reports(self):
        from apps.storage.models import StorageAgreement

        response = self.client.post(
            self.url,
            {"permit_no": "MNE-4711", "asset_length_m": "9.00"},
            follow=True,
        )
        self.assertContains(response, "saved")
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertEqual(agreement.permit_no, "MNE-4711")
        self.assertEqual(agreement.asset_length_m, Decimal("9.00"))

    def test_invalid_input_saves_nothing(self):
        from apps.storage.models import StorageAgreement

        self.client.post(self.url, {"asset_length_m": "not-a-number"})
        self.assertEqual(StorageAgreement.objects.count(), 0)

    def test_deviating_agreed_price_logs_open05(self):
        from apps.storage.models import StoragePriceBand

        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("0"),
            length_max_m=None,
            seasonal_price=Decimal("800"),
        )
        self.client.post(
            self.url,
            {
                "asset_length_m": "9.00",
                "accommodation": "OPEN",
                "term": "SEASONAL",
                "agreed_price": "700",
            },
            follow=True,
        )
        log = ActivityLog.objects.filter(case=self.case).latest("created_at", "pk")
        self.assertIn("OPEN-05", log.text)

    def test_non_storage_case_is_rejected(self):
        from apps.storage.models import StorageAgreement

        build_service_workflow()
        service_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        response = self.client.post(
            reverse("backoffice:storage-docs", args=[service_case.pk]),
            {"permit_no": "MNE-1"},
            follow=True,
        )
        self.assertContains(response, "not a storage case")
        self.assertEqual(StorageAgreement.objects.count(), 0)


class SitePickupViewTests(TestCase):
    """WNT rule 4: the site-pickup card records the pickup on the storage
    agreement from the case page and renders on the case detail."""

    def setUp(self):
        from apps.cases.tests import build_workflow

        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "quote_sent"]
        )
        self.manager = get_user_model().objects.create_user(
            username="pickupmgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            raw_contact_name="Storage Client",
            raw_phone="+44 67 555 111",
        )
        self.client.force_login(self.manager)
        self.url = reverse("backoffice:storage-pickup", args=[self.case.pk])

    def test_post_creates_pickup_and_reports(self):
        from apps.storage.models import SitePickup

        response = self.client.post(
            self.url,
            {
                "organized_by": "SERVICE",
                "is_own_site": "on",
                "lift_out_method": "CRANE",
                "transport_mode": "EXTERNAL_CARRIER",
                "carrier_name": "Acme Logistics Ltd.",
                "status": "SCHEDULED",
            },
            follow=True,
        )
        self.assertContains(response, "Site pickup saved")
        pickup = SitePickup.objects.get(agreement__case=self.case)
        self.assertEqual(pickup.organized_by, "SERVICE")
        self.assertTrue(pickup.is_own_site)
        self.assertEqual(pickup.lift_out_method, "CRANE")
        self.assertEqual(pickup.carrier_name, "Acme Logistics Ltd.")
        log = ActivityLog.objects.filter(case=self.case).latest("created_at", "pk")
        self.assertIn("rule 4", log.text)

    def test_card_renders_on_case_detail(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "Site pickup")

    def test_the_receipt_badge_turns_green_once_the_receipt_exists(self):
        """WNT rule 4: the card shows a "Receipt" badge driven
        by ``SitePickup.intake_record``, but nothing in the backoffice
        ever wrote that FK - the service would have accepted it and the
        field was in ``PICKUP_FIELDS``, yet no view, form or service call
        passed it. So the badge stayed amber ("record below") forever, even
        with a signed receipt for that same case already on file.
        """
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.intake.models import IntakeRecord
        from apps.storage.models import SitePickup

        self.client.post(
            self.url,
            {"organized_by": "SERVICE", "lift_out_method": "CRANE",
             "status": "SCHEDULED"},
        )
        pickup = SitePickup.objects.get(agreement__case=self.case)
        self.assertIsNone(pickup.intake_record_id)

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            self.client.post(
                reverse("backoffice:intake-create", args=[self.case.pk]),
                {
                    "mount_status": "ON_MOUNT",
                    "cleanliness_status": "CLEAN",
                    "storage_location": "Hall 1",
                    "damage_note": "Ogrebotina na desnom boku.",
                    "energy_level": "HALF",  # §7.6
                    "general_condition_note": "OK",
                    "photos": SimpleUploadedFile("bow.jpg", b"fake-jpeg"),
                },
            )
        record = IntakeRecord.objects.get(case=self.case)
        pickup.refresh_from_db()
        self.assertEqual(pickup.intake_record_id, record.pk)

    def test_a_receipt_never_fabricates_a_pickup(self):
        """A case with no site pickup must not grow one as a side effect
        of recording its receipt."""
        import shutil
        import tempfile

        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.storage.models import SitePickup

        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            self.client.post(
                reverse("backoffice:intake-create", args=[self.case.pk]),
                {
                    "mount_status": "ON_MOUNT",
                    "cleanliness_status": "CLEAN",
                    "storage_location": "Hall 1",
                    "damage_note": "No damage.",
                    "energy_level": "HALF",  # §7.6
                    "general_condition_note": "OK",
                    "photos": SimpleUploadedFile("bow.jpg", b"fake-jpeg"),
                },
            )
        self.assertEqual(SitePickup.objects.count(), 0)

    def test_non_storage_case_is_rejected(self):
        from apps.storage.models import SitePickup

        build_service_workflow()
        service_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        response = self.client.post(
            reverse("backoffice:storage-pickup", args=[service_case.pk]),
            {"organized_by": "SERVICE", "status": "PENDING"},
            follow=True,
        )
        self.assertContains(response, "not a storage case")
        self.assertEqual(SitePickup.objects.count(), 0)


class StorageQuotePrefillTests(TestCase):
    """OPEN-05: the price-list suggestion prefills the quote form on the
    storage case page; staff can still edit before creating."""

    def setUp(self):
        from apps.cases.tests import build_workflow
        from apps.storage.models import StorageAgreement, StoragePriceBand

        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "quote_sent"]
        )
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("7"),
            length_max_m=Decimal("10"),
            seasonal_price=Decimal("800"),
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            raw_contact_name="Storage Client",
        )
        StorageAgreement.objects.create(
            case=self.case,
            asset_length_m=Decimal("8.50"),
            accommodation="OPEN",
            term="SEASONAL",
        )
        self.user = get_user_model().objects.create_user(
            username="zimquote", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)

    def test_quote_amount_prefilled_from_band(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, 'value="800.00"')
        self.assertContains(response, "Suggested price")

    def test_service_case_quote_not_prefilled(self):
        build_service_workflow()
        service_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        response = self.client.get(
            reverse("backoffice:case-detail", args=[service_case.pk])
        )
        self.assertNotContains(response, 'value="800.00"')


class StoragePositionViewTests(TestCase):
    """WNT-06: positions are assigned from the case page; exit-order
    conflicts surface as error messages."""

    def setUp(self):
        import datetime as datetime_module

        from apps.cases.tests import build_workflow
        from apps.storage.models import StorageAgreement

        self.datetime = datetime_module
        self.statuses = build_workflow(
            CaseType.STORAGE, ["asset_received", "in_storage"]
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["asset_received"],
            raw_contact_name="Storage Client",
        )
        self.agreement = StorageAgreement.objects.create(
            case=self.case, asset_length_m=Decimal("8.50")
        )
        self.user = get_user_model().objects.create_user(
            username="zimpos", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse(
            "backoffice:storage-position-assign", args=[self.case.pk]
        )

    def test_assign_creates_position(self):
        from apps.storage.models import StoragePosition

        response = self.client.post(
            self.url,
            {"hall": "H1", "row": "A", "slot": "1",
             "planned_exit_date": "2027-04-01"},
            follow=True,
        )
        self.assertContains(response, "assigned")
        position = StoragePosition.objects.get()
        self.assertEqual(position.hall, "H1")
        self.assertEqual(str(position.planned_exit_date), "2027-04-01")

    def test_wnt06_conflict_surfaces_error(self):
        """A *different* asset parked in front, leaving later, blocks us.

        This used to seed the blocking position on ``self.agreement`` -
        the very agreement doing the posting - so what it actually
        asserted was that a case conflicted with itself. That passed only
        because ``assign_position`` appended rows instead of updating the
        agreement's own, which is the bug fixed alongside this test.
        """
        from apps.storage.models import StorageAgreement, StoragePosition
        from apps.storage.services import assign_position

        neighbour_case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["asset_received"],
            raw_contact_name="Susjed",
        )
        neighbour = StorageAgreement.objects.create(
            case=neighbour_case, asset_length_m=Decimal("7.00")
        )
        assign_position(neighbour, "H1", "A", 1, self.datetime.date(2027, 4, 1))

        response = self.client.post(
            self.url,
            {"hall": "H1", "row": "A", "slot": "2",
             "planned_exit_date": "2027-03-01"},
            follow=True,
        )
        self.assertContains(response, "WNT-06")
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_the_position_can_be_corrected_afterwards(self):
        """The card has to stay usable after the first save.

        Re-submitting the same slot with a new exit date was refused as a
        conflict with the row just written, and picking a different slot
        appended a second row - one asset in two places at once.
        """
        from apps.storage.models import StoragePosition

        self.client.post(
            self.url,
            {"hall": "H1", "row": "A", "slot": "1",
             "planned_exit_date": "2027-04-01"},
            follow=True,
        )
        response = self.client.post(
            self.url,
            {"hall": "H1", "row": "A", "slot": "1",
             "planned_exit_date": "2027-05-15"},
            follow=True,
        )
        self.assertNotContains(response, "WNT-06")
        position = StoragePosition.objects.get()
        self.assertEqual(str(position.planned_exit_date), "2027-05-15")

        self.client.post(
            self.url,
            {"hall": "H2", "row": "B", "slot": "3",
             "planned_exit_date": "2027-05-15"},
            follow=True,
        )
        position = StoragePosition.objects.get()
        self.assertEqual((position.hall, position.row, position.slot),
                         ("H2", "B", "3"))

    def test_requires_an_agreement(self):
        self.agreement.delete()
        response = self.client.post(
            self.url, {"hall": "H1", "row": "A", "slot": "1"}, follow=True
        )
        self.assertContains(
            response, "Record the storage documentation first"
        )


class StoragePricingViewTests(TestCase):
    """OPEN-05: the backoffice price list - any staff may view, only
    MANAGER/DIRECTOR may change; validate_band errors surface."""

    def setUp(self):
        from apps.storage.models import StoragePriceBand

        self.band = StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("0"),
            length_max_m=Decimal("10"),
            seasonal_price=Decimal("500"),
        )
        User = get_user_model()
        self.frontdesk = User.objects.create_user(
            username="cjfront", password="demo12345",
            is_staff=True, role="FRONT_DESK",
        )
        self.manager = User.objects.create_user(
            username="cjmgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.page = reverse("backoffice:section-storage-pricing")

    def test_any_staff_views_but_cannot_edit(self):
        self.client.force_login(self.frontdesk)
        response = self.client.get(self.page)
        self.assertContains(response, "500")
        self.assertNotContains(response, "band-form-new")
        self.assertContains(response, "Only a manager or director")

    def test_frontdesk_change_is_refused(self):
        from apps.storage.models import StoragePriceBand

        self.client.force_login(self.frontdesk)
        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "OPEN", "new-length_min_m": "10",
             "new-length_max_m": "", "new-seasonal_price": "900"},
            follow=True,
        )
        self.assertContains(response, "may not edit")
        self.assertEqual(StoragePriceBand.objects.count(), 1)

    def test_manager_add_edit_delete(self):
        from apps.storage.models import StoragePriceBand

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "OPEN", "new-length_min_m": "10",
             "new-length_max_m": "", "new-seasonal_price": "900"},
            follow=True,
        )
        self.assertContains(response, "added")
        new_band = StoragePriceBand.objects.get(length_min_m=Decimal("10"))

        prefix = "band-%s" % self.band.pk
        response = self.client.post(
            reverse("backoffice:pricing-band-edit", args=[self.band.pk]),
            {
                "%s-accommodation" % prefix: "OPEN",
                "%s-length_min_m" % prefix: "0",
                "%s-length_max_m" % prefix: "10",
                "%s-daily_price" % prefix: "25",
                "%s-seasonal_price" % prefix: "550",
            },
            follow=True,
        )
        self.assertContains(response, "updated")
        self.band.refresh_from_db()
        self.assertEqual(self.band.seasonal_price, Decimal("550"))
        self.assertEqual(self.band.daily_price, Decimal("25"))
        self.assertIsNone(self.band.monthly_price)

        response = self.client.post(
            reverse("backoffice:pricing-band-delete", args=[new_band.pk]),
            follow=True,
        )
        self.assertContains(response, "deleted")
        self.assertEqual(StoragePriceBand.objects.count(), 1)

    def test_overlapping_band_surfaces_error(self):
        from apps.storage.models import StoragePriceBand

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "OPEN", "new-length_min_m": "5",
             "new-length_max_m": "12", "new-seasonal_price": "700"},
            follow=True,
        )
        self.assertContains(response, "overlaps")
        self.assertEqual(StoragePriceBand.objects.count(), 1)

    def test_same_range_other_accommodation_is_accepted(self):
        from apps.storage.models import StoragePriceBand

        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "INDOOR", "new-length_min_m": "5",
             "new-length_max_m": "12", "new-seasonal_price": "700"},
            follow=True,
        )
        self.assertContains(response, "added")
        self.assertEqual(StoragePriceBand.objects.count(), 2)


class StorageListColumnTests(TestCase):
    """The storage section: 'New inquiry' entry point, documentation
    badge and the price-list price per row."""

    def setUp(self):
        from apps.storage.models import StorageAgreement, StoragePriceBand

        self.status = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="inquiry",
            name="Upit",
            client_visible_name="Received",
        )
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("7"),
            length_max_m=Decimal("10"),
            seasonal_price=Decimal("800"),
        )
        self.documented = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
            raw_contact_name="Dokumentovani",
            raw_phone="+44 67 1",
        )
        StorageAgreement.objects.create(
            case=self.documented,
            asset_length_m=Decimal("8.50"),
            permit_no="MNE-1",
            accommodation="OPEN",
            term="SEASONAL",
        )
        self.bare = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
            raw_contact_name="Nedokumentovani",
        )
        self.user = get_user_model().objects.create_user(
            username="zimlist", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)

    def test_columns_and_buttons(self):
        response = self.client.get(
            reverse("backoffice:section-storage")
        )
        self.assertContains(response, "New inquiry")
        self.assertContains(response, "?case_type=STORAGE")
        self.assertContains(
            response, reverse("backoffice:section-storage-pricing")
        )
        self.assertContains(response, "complete")
        self.assertContains(response, "incomplete")
        self.assertContains(response, "800")


class StorageOfferViewTests(TestCase):
    """WNT-03: the offer card on the storage case - renewal banner for
    an existing package user, recommendation for a new client, and the
    offer POST that drafts the quote and records the choice."""

    def setUp(self):
        import datetime

        from django.apps import apps as django_apps

        from apps.cases.tests import build_workflow
        from apps.storage.models import PackageComponent, StoragePackage

        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "quote_sent"]
        )
        Client = django_apps.get_model("clients", "Client")
        self.client_obj = Client.objects.create(name="Storage Client")
        self.package = StoragePackage.objects.create(
            name="Package deal",
            season_start=datetime.date(2025, 10, 15),
            season_end=datetime.date(2026, 10, 15),
        )
        self.package.components.add(
            PackageComponent.objects.create(name="Storage", sort_order=0)
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["inquiry"],
            client=self.client_obj,
        )
        self.user = get_user_model().objects.create_user(
            username="zimoffer", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse("backoffice:storage-offer", args=[self.case.pk])

    def test_new_client_sees_recommendation_banner(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        # The rule ID is deliberately not in the banner: user-facing labels
        # carry the advice, not the internal tag it came from.
        self.assertContains(response, "the package arrangement is the recommended option")
        self.assertNotContains(response, "WNT-03")

    def test_existing_package_user_sees_renewal_banner(self):
        from apps.storage.models import StorageAgreement

        old_status = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="inquiry_old",
            name="inquiry",
            client_visible_name="inquiry",
        )
        old_case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=old_status,
            client=self.client_obj,
        )
        StorageAgreement.objects.create(case=old_case, package=self.package)
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "Existing package user")

    def test_post_creates_package_offer(self):
        import datetime

        from apps.quotes.models import Quote
        from apps.storage.models import StorageAgreement

        response = self.client.post(
            self.url,
            {
                "offer_type": "package",
                "package": self.package.pk,
                "period_start": "2026-10-15",
                "period_end": "2027-10-15",
                "description": "Package deal.",
                "amount": "4000",
            },
            follow=True,
        )
        self.assertContains(response, "Offer quote")
        quote = Quote.objects.get(case=self.case)
        self.assertIn("Commercial period:", quote.description)
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertEqual(agreement.package, self.package)
        self.assertEqual(
            agreement.period_start, datetime.date(2026, 10, 15)
        )

    def test_post_standalone_offer_clears_package(self):
        from apps.quotes.models import Quote
        from apps.storage.models import StorageAgreement

        StorageAgreement.objects.create(case=self.case, package=self.package)
        self.client.post(
            self.url,
            {
                "offer_type": "standalone",
                "description": "Samo storage.",
                "amount": "800",
            },
            follow=True,
        )
        self.assertEqual(Quote.objects.filter(case=self.case).count(), 1)
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertIsNone(agreement.package)

    def test_package_offer_without_package_is_rejected(self):
        from apps.quotes.models import Quote

        response = self.client.post(
            self.url,
            {
                "offer_type": "package",
                "description": "x",
                "amount": "1",
            },
            follow=True,
        )
        self.assertContains(response, "package offer needs a package")
        self.assertEqual(Quote.objects.filter(case=self.case).count(), 0)


class StorageLayoutViewTests(TestCase):
    """WNT-05: the graphic yard page and click-to-place endpoint."""

    def setUp(self):
        from apps.cases.tests import build_workflow
        from apps.storage.models import StorageAgreement, StorageZone

        self.statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "contracted", "closed"]
        )
        self.zone = StorageZone.objects.create(
            name="Open yard",
            width_m=Decimal("40"),
            length_m=Decimal("25"),
        )
        # QA S-H3: an inquiry takes no ground on the yard.
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=self.statuses["contracted"],
        )
        StorageAgreement.objects.create(
            case=self.case,
            asset_length_m=Decimal("10"),
            asset_width_m=Decimal("4"),
        )
        self.user = get_user_model().objects.create_user(
            username="yard", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)

    def test_layout_page_lists_zone(self):
        response = self.client.get(reverse("backoffice:storage-layout"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open yard")

    def test_placing_mode_shows_form(self):
        response = self.client.get(
            reverse("backoffice:storage-layout"), {"case": self.case.pk}
        )
        self.assertContains(response, "Place asset")

    def test_place_creates_position(self):
        from apps.storage.models import StoragePosition

        response = self.client.post(
            reverse("backoffice:storage-place", args=[self.case.pk]),
            {
                "zone": self.zone.pk,
                "pos_x_m": "0",
                "pos_y_m": "0",
                "orientation": "LENGTHWISE",
            },
            follow=True,
        )
        self.assertContains(response, "Asset placed on the yard")
        position = StoragePosition.objects.get(agreement__case=self.case)
        self.assertTrue(position.is_coordinate_placed)
        self.assertEqual(position.zone, self.zone)

    def test_out_of_bounds_surfaces_error(self):
        from apps.storage.models import StoragePosition

        response = self.client.post(
            reverse("backoffice:storage-place", args=[self.case.pk]),
            {
                "zone": self.zone.pk,
                "pos_x_m": "39",
                "pos_y_m": "0",
                "orientation": "LENGTHWISE",
            },
            follow=True,
        )
        self.assertContains(response, "does not fit")
        self.assertEqual(StoragePosition.objects.count(), 0)


class TransportLoadingViewTests(TestCase):
    """TRP-07: the internal loading confirmation is recorded from the
    backoffice case page and stamps the responsible person's sign-off."""

    def setUp(self):
        from apps.transport.models import LoadingConfirmation, TransportOrder

        self.LoadingConfirmation = LoadingConfirmation
        status = Status.objects.create(
            case_type=CaseType.TRANSPORT,
            code="in_transit",
            name="in_transit",
            client_visible_name="in_transit",
        )
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            status=status,
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Test Contact",
        )
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        # TRP-06: the intake record is made before loading, so the loading
        # card refuses to sign without one.
        from apps.intake.models import IntakeRecord
        IntakeRecord.objects.create(case=self.case, signed_at=timezone.now())
        self.user = get_user_model().objects.create_user(
            username="loader", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse("backoffice:transport-loading", args=[self.case.pk])

    def test_post_records_confirmation_and_signs(self):
        response = self.client.post(
            self.url,
            {
                "vehicle_type": self.LoadingConfirmation.VehicleType.TRAILER,
                "readiness": self.LoadingConfirmation.Readiness.READY,
                "lifting_equipment_used": "on",
                "carrier": "Carrier Ltd",
                "client_notified": "on",
                "notes": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        confirmation = self.order.loading_confirmation
        self.assertEqual(
            confirmation.vehicle_type,
            self.LoadingConfirmation.VehicleType.TRAILER,
        )
        self.assertTrue(confirmation.lifting_equipment_used)
        self.assertTrue(confirmation.client_notified)
        self.assertEqual(confirmation.signed_by, self.user)
        self.assertIsNotNone(confirmation.signed_at)
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="TRP-07"
            ).exists()
        )


class FiveMinuteDateTimeWidgetTests(SimpleTestCase):
    """Scheduling inputs offer a five-minute grid as a menu, so an
    off-grid minute cannot be entered and then rejected - and, on the
    site's own appointments, only the grid inside opening hours."""

    def _widget_html(self, initial=None):
        form = AppointmentBookForm(
            initial={"scheduled_for": initial} if initial else None
        )
        return str(form["scheduled_for"])

    def test_only_five_minute_slots_are_offered(self):
        html = self._widget_html()
        self.assertIn(">10:00<", html)
        self.assertIn(">10:05<", html)
        self.assertNotIn(">10:01<", html)
        self.assertNotIn(">10:19<", html)
        # One slot per five minutes across the widest opening window
        # (Sat 08:00 to weekday 17:00), plus the "--:--" placeholder.
        self.assertEqual(
            html.count("<option"), len(workinghours.all_slots()) + 1
        )
        # No free-text minute entry is left anywhere in the markup.
        self.assertNotIn("datetime-local", html)

    def test_menu_stops_at_the_site_opening_hours(self):
        """SVC-04: a time the yard is shut is not on the menu at all."""
        html = self._widget_html()
        self.assertIn(">08:00<", html)   # Saturday opening
        self.assertIn(">17:00<", html)   # weekday close, inclusive
        self.assertNotIn(">07:55<", html)
        self.assertNotIn(">17:05<", html)
        self.assertNotIn(">03:00<", html)

    def test_per_weekday_windows_are_published_to_the_browser(self):
        """The date half drives the narrowing client-side, so the
        windows travel with the widget."""
        html = self._widget_html()
        self.assertIn("data-working-hours", html)
        self.assertIn("09:00", html)
        # Sunday is closed - a null window, not an omitted key.
        self.assertIn("&quot;6&quot;:null", html)

    def test_carrier_windows_keep_the_full_grid(self):
        """TRP-02: an external carrier's loading time is not the
        site's to keep, so it stays on the 24-hour grid."""
        widget = FiveMinuteDateTimeWidget(working_hours=False)
        html = widget.render("loading_at", None, {})
        self.assertIn(">03:00<", html)
        self.assertEqual(html.count("<option"), 24 * 12 + 1)
        self.assertNotIn("data-working-hours", html)

    def test_date_and_slot_are_submitted_as_one_value(self):
        form = AppointmentBookForm(
            {
                "kind": Appointment.Kind.INTAKE,
                "scheduled_for_0": "2027-08-04",
                "scheduled_for_1": "10:15",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        scheduled = timezone.localtime(form.cleaned_data["scheduled_for"])
        self.assertEqual(
            (scheduled.year, scheduled.month, scheduled.day),
            (2027, 8, 4),
        )
        self.assertEqual((scheduled.hour, scheduled.minute), (10, 15))

    def test_flat_datetime_local_value_still_accepted(self):
        form = AppointmentBookForm(
            {
                "kind": Appointment.Kind.INTAKE,
                "scheduled_for": "2027-08-04T10:15",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_off_grid_stored_value_stays_selectable(self):
        html = self._widget_html(
            timezone.make_aware(datetime(2027, 8, 4, 10, 19))
        )
        self.assertIn('value="2027-08-04"', html)
        self.assertIn('value="10:19" selected', html)

    def test_out_of_hours_stored_value_stays_selectable(self):
        """A time booked before these hours existed must not be moved
        silently by the act of re-saving the record."""
        html = self._widget_html(
            timezone.make_aware(datetime(2027, 8, 4, 21, 30))
        )
        self.assertIn('value="21:30" selected', html)


class Section4CardTests(CaseDetailViewTestBase):
    """Procedure §4 on the case page: what the cards refuse to offer."""

    def setUp(self):
        super().setUp()
        from apps.service.models import WorkOrder

        self.client.force_login(self.manager)
        self.case.status = self.statuses["in_progress"]
        self.case.save()
        self.work_order = WorkOrder.objects.create(case=self.case)

    def test_the_work_forms_are_replaced_while_the_order_is_preliminary(self):
        """SVC-15/§4.1: since SVC-02 opens a preliminary order at triage,
        this card is on screen from the first minute of a case."""
        response = self.client.get(self._detail_url())
        self.assertContains(response, "still preliminary")
        self.assertNotContains(
            response, reverse("backoffice:work-log-add", args=[self.case.pk])
        )

    def test_the_work_forms_come_back_once_it_is_endorsed(self):
        from apps.service.services import finalize_work_order

        finalize_work_order(self.work_order, self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(
            response, reverse("backoffice:work-log-add", args=[self.case.pk])
        )

    def test_the_completion_form_prefills_what_the_order_records(self):
        """SVC-17/§4.3: nobody should have to do the arithmetic that the
        work order already contains."""
        from apps.service.models import ServicePriceItem
        from apps.service.services import finalize_work_order, log_work

        ServicePriceItem.objects.create(
            category=ServicePriceItem.Category.LABOR,
            name="Radni sat",
            price=Decimal("50"),
            unit=ServicePriceItem.Unit.PER_HOUR,
        )
        finalize_work_order(self.work_order, self.manager)
        log_work(
            self.work_order, self.manager, description="x", hours=Decimal("3")
        )
        self.case.status = self.statuses["ready_for_handover"]
        self.case.save()
        response = self.client.get(self._detail_url())
        self.assertContains(response, "150")
        self.assertContains(response, "needs a reason")

    def test_a_test_run_posts_the_control_and_the_finding(self):
        """SVC-16/§4.2."""
        from apps.service.models import TestRun

        self.case.status = self.statuses["quality_check"]
        self.case.save()
        response = self.client.post(
            reverse("backoffice:test-run-record", args=[self.case.pk]),
            {
                "kind": "FIELD_TEST",
                "result": "PASS",
                "fault_cleared": "on",
                "notes": "Field-tested, ran 20 minutes under load.",
            },
            follow=True,
        )
        self.assertContains(response, "PASSED")
        run = TestRun.objects.get(work_order=self.work_order)
        self.assertEqual(run.kind, TestRun.Kind.FIELD_TEST)
        self.assertTrue(run.fault_cleared)

    def test_a_pass_without_the_fault_finding_is_rejected(self):
        self.case.status = self.statuses["quality_check"]
        self.case.save()
        response = self.client.post(
            reverse("backoffice:test-run-record", args=[self.case.pk]),
            {"kind": "TEST_RUN", "result": "PASS", "notes": "Ran fine."},
            follow=True,
        )
        self.assertContains(response, "reported fault")

    def test_the_field_test_option_is_absent_on_an_in_water_case(self):
        """SVC-03: the asset was never lifted out, so it cannot be
        field-tested - the service refuses it and the menu does not offer it."""
        from apps.cases.services import record_work_location_consult

        record_work_location_consult(self.case, "ON_SITE", self.manager)
        self.case.status = self.statuses["quality_check"]
        self.case.save()
        response = self.client.get(self._detail_url())
        self.assertNotContains(response, 'value="FIELD_TEST"')
        self.assertContains(response, 'value="TEST_RUN"')


class WorkOrderReopenViewTests(CaseDetailViewTestBase):
    """SVC-20/§4.5: undoing a close is a management act with a reason."""

    def setUp(self):
        super().setUp()
        from apps.cases.models import WorkLocation
        from apps.service.models import WorkOrder
        from apps.service.services import close_work_order

        self.case.status = self.statuses["ready_for_handover"]
        self.case.work_location = WorkLocation.ON_SITE
        self.case.financially_settled = True
        self.case.save()
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )
        close_work_order(self.work_order, self.manager)

    def _reopen(self, user, reason="Impeller billed to the wrong order"):
        self.client.force_login(user)
        return self.client.post(
            reverse("backoffice:work-order-reopen", args=[self.case.pk]),
            {"reason": reason},
            follow=True,
        )

    def test_a_manager_may_reopen_it(self):
        response = self._reopen(self.manager)
        self.assertContains(response, "reopened")
        self.work_order.refresh_from_db()
        self.assertIsNone(self.work_order.closed_at)

    def test_a_technician_may_not(self):
        response = self._reopen(self.technician)
        self.assertContains(response, "may not reopen")
        self.work_order.refresh_from_db()
        self.assertIsNotNone(self.work_order.closed_at)

    def test_the_reason_lands_on_the_field_when_missing(self):
        response = self._reopen(self.manager, reason="")
        self.assertContains(response, "correct the highlighted fields")
        self.work_order.refresh_from_db()
        self.assertIsNotNone(self.work_order.closed_at)

    def test_the_closed_notice_is_on_the_closing_card(self):
        self.client.force_login(self.manager)
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Work order closed on")


class OnSiteClosingChecklistTests(CaseDetailViewTestBase):
    """SVC-03 regression: narrowing the handover card must not take the
    closing checklist with it.

    An in-water case owes no handover record, but SVC-19 still asks for
    financial settlement before it can close - and the toggle for that
    lives on the closing checklist. Gating both cards on one flag left
    such a case unsettleable, and therefore uncloseable, from the UI.
    """

    def setUp(self):
        super().setUp()
        from apps.cases.models import WorkLocation

        self.case.status = self.statuses["ready_for_handover"]
        self.case.work_location = WorkLocation.ON_SITE
        self.case.save()
        self.client.force_login(self.manager)

    def test_the_closing_checklist_is_there_without_a_handover(self):
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Closing checklist")
        self.assertContains(response, "Mark financially settled")
        self.assertNotContains(response, "Handover vs. intake comparison")

    def test_settlement_can_still_be_flipped(self):
        self.client.post(
            reverse("backoffice:case-settlement", args=[self.case.pk]),
            {"settled": "1"},
            follow=True,
        )
        self.case.refresh_from_db()
        self.assertTrue(self.case.financially_settled)

    def test_a_yard_case_still_shows_both(self):
        self.case.work_location = ""
        self.case.save()
        response = self.client.get(self._detail_url())
        self.assertContains(response, "Handover vs. intake comparison")
        self.assertContains(response, "Closing checklist")


class PartsPickerViewTests(CaseDetailViewTestBase):
    """SVC-10 on the diagnostic card: pick from the catalogue, describe
    only what it has no row for."""

    def setUp(self):
        super().setUp()
        self.case.status = self.statuses["asset_received"]
        self.case.save()
        self.work_order = WorkOrder.objects.create(
            case=self.case, is_preliminary=False
        )
        self.stocked = Part.objects.create(
            code="IMP-1", name="Impeller kit", is_consumable=True, stock_qty=4
        )
        self.empty = Part.objects.create(
            code="GBX-9", name="Gearbox", is_consumable=False, stock_qty=0
        )
        self.client.force_login(self.manager)

    def _post_diagnostic(self, **extra):
        data = {
            "issue": "Impeller worn",
            "servicing_work": "Replace it",
            "estimated_parts_price": "40.00",
            "estimated_completion_date": "2027-01-10",
        }
        data.update(extra)
        return self.client.post(
            reverse("backoffice:diagnostic-create", args=[self.case.pk]),
            data,
            follow=True,
        )

    def test_the_picker_offers_the_whole_catalogue_with_stock_shown(self):
        """Availability is not identity: an out-of-stock part the
        catalogue knows is still offered, annotated rather than hidden."""
        response = self.client.get(self._detail_url())
        self.assertContains(response, "IMP-1")
        self.assertContains(response, "GBX-9")
        self.assertContains(response, "in stock: 4")
        self.assertContains(response, "out of stock")
        self.assertContains(response, "not a consumable")

    def test_ticked_parts_are_recorded_with_their_quantity(self):
        response = self._post_diagnostic(
            **{
                "needed_parts": [str(self.stocked.pk)],
                "qty_%s" % self.stocked.pk: "3",
            }
        )
        self.assertContains(response, "recorded")
        needed = DiagnosticPart.objects.get()
        self.assertEqual(needed.part, self.stocked)
        self.assertEqual(needed.quantity, 3)

    def test_the_text_box_alone_is_still_accepted(self):
        response = self._post_diagnostic(parts_needed="Custom bracket")
        self.assertContains(response, "recorded")
        self.assertEqual(DiagnosticPart.objects.count(), 0)

    def test_answering_neither_lands_on_the_field(self):
        response = self._post_diagnostic()
        self.assertContains(response, "correct the highlighted fields")
        self.assertContains(response, "no row for")
        self.assertEqual(Diagnostic.objects.count(), 0)

    def test_a_blank_quantity_means_one(self):
        self._post_diagnostic(**{"needed_parts": [str(self.stocked.pk)]})
        self.assertEqual(DiagnosticPart.objects.get().quantity, 1)

    def test_the_recorded_diagnostic_shows_both_halves(self):
        self._post_diagnostic(
            **{
                "needed_parts": [str(self.stocked.pk)],
                "qty_%s" % self.stocked.pk: "2",
                "parts_needed": "Custom bracket",
            }
        )
        response = self.client.get(self._detail_url())
        self.assertContains(response, "IMP-1")
        self.assertContains(response, "not in the catalogue")
        self.assertContains(response, "Custom bracket")


class PartQuickAddViewTests(CaseDetailViewTestBase):
    """SVC-10: the free-text half is a queue, not a resting place - a part
    with no catalogue row can never be ordered, installed or priced."""

    def setUp(self):
        super().setUp()
        WorkOrder.objects.create(case=self.case, is_preliminary=False)
        self.client.force_login(self.manager)

    def _add(self, **extra):
        data = {
            "code": "BRK-1",
            "name": "Custom bracket",
            "stock_qty": "2",
            "is_consumable": "on",
            "unit_price": "35.00",
        }
        data.update(extra)
        return self.client.post(
            reverse("backoffice:part-quick-add", args=[self.case.pk]),
            data,
            follow=True,
        )

    def test_it_mints_a_catalogue_row_and_offers_it_next_time(self):
        response = self._add()
        self.assertContains(response, "added to the catalogue")
        part = Part.objects.get(code="BRK-1")
        self.assertTrue(part.is_consumable)
        self.assertEqual(part.stock_qty, 2)
        self.assertEqual(part.unit_price, Decimal("35.00"))
        # The point of the whole thing: it is selectable from now on.
        self.assertContains(response, "BRK-1")

    def test_a_duplicate_code_is_refused_on_the_field(self):
        Part.objects.create(
            code="BRK-1", name="Existing", is_consumable=True, stock_qty=1
        )
        response = self._add()
        self.assertContains(response, "already in the catalogue")
        self.assertEqual(Part.objects.filter(code="BRK-1").count(), 1)

    def test_a_price_is_optional_here(self):
        """SVC-14 blocks on it later; refusing it here would just stop
        somebody getting the part into the catalogue at all."""
        self._add(code="BRK-2", unit_price="")
        self.assertIsNone(Part.objects.get(code="BRK-2").unit_price)
