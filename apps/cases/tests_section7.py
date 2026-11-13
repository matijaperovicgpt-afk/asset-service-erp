"""Procedure §7 "Mandatory data in the service ticket and work order".

One class per mandatory datum that was named in the procedure and asked
for by nothing.

Phase 1 covers §7's fifth bullet, *delivery method*, which §8 names a
second time as half of what "Intake scheduled" means. Phase 3 covers the
fourth bullet, *symptom description and the client's assessment of the
fault* - two data
carried by one field, the second of which had no field at all.

Phase 5 covers the third bullet's *report location* - written by both
creation paths since they were written, and read only for display.

Phase 6 covers the first bullet, *reporter, owner and contact
person* - three roles for which the application had two homes, and whose
absence quietly aimed every client e-mail at whoever filed the report.

Phase 2 (the handover photographs) and phase 5's fuel gauge live in
``apps/intake/tests_section7.py``; phase 4 (the asset data) in
``apps/clients/tests_section7.py``; phase 6's notification routing in
``apps/notifications/tests_section7.py``.

The adversarial follow-up classes hold holes found by probing the
finished phases rather than by reading the procedure again. Each names
the phase it belongs to; the last three came from a second pass that
probed the FIXES, and one of them is a regression the first pass
introduced.
"""
import tempfile
from datetime import timedelta

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.cases.exceptions import GuardCheckFailed
from apps.cases.models import ContactRole, DeliveryMethod, WorkLocation
from apps.cases.services import (
    auto_advance_case,
    record_client_assessment,
    record_contact_person,
    record_delivery_method,
    record_report_location,
    record_work_location_consult,
    transition_case,
)
from apps.cases.tests import build_service_workflow, build_workflow


class DeliveryMethodMixin:
    """A SERVICE case at ``new_report`` with the full seeded chain."""

    def setUp(self):
        super().setUp()
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            # §7.1: the same guard asks who we contact; the reporter is
            # the answer here, and phase 6 tests the case where it is not.
            contact_role=ContactRole.REPORTER,
            # §7.3: the intake_scheduled guard asks where the asset is.
            location="Customer site",
        )

    def book_intake(self):
        Appointment = django_apps.get_model("scheduling", "Appointment")
        return Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.INTAKE,
            scheduled_for=timezone.now() + timedelta(days=2),
        )


class DeliveryMethodGuardTests(DeliveryMethodMixin, TestCase):
    """§7.5 / §8 "Intake scheduled": *date and arrival method
    agreed.*

    SVC-04's rule text and `_guard_intake_scheduled`'s own docstring have
    both said "date and delivery method" from the start. Only the date
    was ever checked, because there was no field for the other half.
    """

    def test_the_location_is_asked_for_before_the_delivery_method(self):
        # §7.3 comes first: you cannot agree who collects an asset from
        # where until somebody says where.
        self.case.location = ""
        self.case.save(update_fields=["location"])
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        self.book_intake()
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("where the asset is", str(ctx.exception))

    def test_the_date_alone_no_longer_schedules_the_intake(self):
        self.book_intake()
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("SVC-04", str(ctx.exception))
        self.assertIn("the way the asset gets here", str(ctx.exception))

    def test_the_missing_appointment_is_still_reported_first(self):
        # The status is named after the date, and "no appointment" is the
        # more fundamental answer - so it is the one staff see first.
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("intake appointment", str(ctx.exception))

    def test_both_halves_together_schedule_the_intake(self):
        self.book_intake()
        record_delivery_method(
            self.case, DeliveryMethod.SERVICE_PICKUP, self.user
        )
        transition_case(self.case, "intake_scheduled", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "intake_scheduled")

    def test_an_on_site_case_has_no_delivery_to_agree(self):
        # SVC-03: nothing is delivered anywhere, so the question does not
        # apply and the guard must not invent it.
        record_work_location_consult(
            self.case, WorkLocation.ON_SITE, self.user
        )
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.ON_SITE_VISIT,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        transition_case(self.case, "intake_scheduled", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "intake_scheduled")
        self.assertEqual(self.case.delivery_method, "")


class RecordDeliveryMethodServiceTests(DeliveryMethodMixin, TestCase):
    """The service that writes it."""

    def test_records_and_logs(self):
        record_delivery_method(
            self.case,
            DeliveryMethod.EXTERNAL_CARRIER,
            self.user,
            note="Client arranged Carrier Ltd.",
        )
        self.case.refresh_from_db()
        self.assertEqual(
            self.case.delivery_method, DeliveryMethod.EXTERNAL_CARRIER
        )
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("SVC-04", entry.text)
        self.assertIn("Client arranged Carrier Ltd.", entry.text)

    def test_a_change_is_logged_as_a_change(self):
        # A client who said they would trailer it and then asks us to
        # collect it is an ordinary Tuesday - but whoever booked the
        # crane needs to see it happened.
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        record_delivery_method(
            self.case, DeliveryMethod.SERVICE_PICKUP, self.user
        )
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        texts = [
            e.text
            for e in ActivityLog.objects.filter(case=self.case).order_by("pk")
        ]
        self.assertEqual(len(texts), 2)
        self.assertIn("recorded as", texts[0])
        self.assertIn("changed from", texts[1])

    def test_re_recording_the_same_answer_is_silent(self):
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertEqual(ActivityLog.objects.filter(case=self.case).count(), 1)

    def test_an_unknown_method_is_refused(self):
        with self.assertRaises(ValidationError):
            record_delivery_method(self.case, "HELICOPTER", self.user)

    def test_refused_on_a_case_worked_where_the_asset_lies(self):
        record_work_location_consult(
            self.case, WorkLocation.IN_FIELD, self.user
        )
        with self.assertRaises(ValidationError) as ctx:
            record_delivery_method(
                self.case, DeliveryMethod.CLIENT_SHIPS, self.user
            )
        self.assertIn("nothing is delivered anywhere", str(ctx.exception))


class DeliveryMethodScreenTests(DeliveryMethodMixin, TestCase):
    """It has to be recordable from the case page, not only the shell."""

    def setUp(self):
        super().setUp()
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        self.client.force_login(self.user)

    def test_the_card_posts_from_the_case_page(self):
        from django.urls import reverse

        response = self.client.post(
            reverse("backoffice:case-delivery-method", args=[self.case.pk]),
            {
                "delivery_method": DeliveryMethod.CLIENT_DROP_OFF,
                "note": "Owner sails it round on Friday.",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.delivery_method, DeliveryMethod.CLIENT_DROP_OFF)

    def test_the_card_renders_on_a_case_that_expects_the_asset(self):
        from django.urls import reverse

        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "delivery-method/")
        self.assertContains(response, 'name="delivery_method"')

    def test_the_card_is_withdrawn_on_an_on_site_case(self):
        from django.urls import reverse

        record_work_location_consult(
            self.case, WorkLocation.ON_SITE, self.user
        )
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertNotContains(response, 'name="delivery_method"')


class DeliveryMethodGuideTests(DeliveryMethodMixin, TestCase):
    """The wizard mirrors the guard rather than contradicting it."""

    def _guide(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self.case.refresh_from_db()
        return build_guide(self.case, self.user, ctx_for(self.case))

    def _step(self, guide, key):
        for step in guide["steps"]:
            if step.key == key:
                return step
        return None

    def test_the_step_is_listed_and_blocking_until_answered(self):
        guide = self._guide()
        step = self._step(guide, "record_delivery_method")
        self.assertIsNotNone(step)
        self.assertFalse(step.done)
        self.assertTrue(step.blocking)

    def test_the_step_ticks_once_answered(self):
        record_delivery_method(
            self.case, DeliveryMethod.ALREADY_ON_SITE, self.user
        )
        step = self._step(self._guide(), "record_delivery_method")
        self.assertTrue(step.done)

    def test_the_step_drops_off_an_on_site_case(self):
        record_work_location_consult(
            self.case, WorkLocation.ON_SITE, self.user
        )
        self.assertIsNone(
            self._step(self._guide(), "record_delivery_method")
        )


class ClientAssessmentMixin:
    """A SERVICE case sitting at ``asset_received``, ready to diagnose."""

    def setUp(self):
        super().setUp()
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p3", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["asset_received"],
            raw_contact_name="John Smith",
            symptoms_description="Engine cuts out above 2000 rpm.",
        )

    def start_diagnostics(self):
        return transition_case(self.case, "diagnostics", self.user)


class ClientAssessmentGuardTests(ClientAssessmentMixin, TestCase):
    """§7.4 / SVC-01: *symptom description and the client's assessment of the fault.*

    SVC-01 has named the client's own assessment since it was
    written. There was no field for it, and the symptom description it
    is paired with was enforced by two forms and by nothing else - so
    anyone reaching the case another way was under no obligation at all.
    """

    def test_diagnostics_refused_until_the_client_is_asked(self):
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.start_diagnostics()
        self.assertIn("SVC-01", str(ctx.exception))
        self.assertIn("what they think is wrong", str(ctx.exception))

    def test_the_missing_symptom_description_is_reported_first(self):
        # It is the more fundamental half: without it there is no report
        # at all, only a guess about an asset nobody described.
        self.case.symptoms_description = ""
        self.case.save(update_fields=["symptoms_description"])
        record_client_assessment(self.case, self.user, gave_no_assessment=True)
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.start_diagnostics()
        self.assertIn("what the asset is doing", str(ctx.exception))

    def test_an_assessment_opens_the_gate(self):
        record_client_assessment(
            self.case, self.user, assessment="Fuel filter, probably."
        )
        self.start_diagnostics()
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "diagnostics")

    def test_no_view_is_also_an_answer(self):
        # Asked and blank is a fact about the report; never asked is a
        # hole in it. The gate accepts the first and refuses the second.
        record_client_assessment(self.case, self.user, gave_no_assessment=True)
        self.start_diagnostics()
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "diagnostics")

    def test_a_non_service_case_is_not_asked_about_faults(self):
        Case = django_apps.get_model("cases", "Case")
        statuses = build_workflow("TRANSPORT", ["new_report", "diagnostics"], [])
        case = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=statuses["new_report"],
        )
        transition_case(case, "diagnostics", self.user)
        case.refresh_from_db()
        self.assertEqual(case.status.code, "diagnostics")


class RecordClientAssessmentServiceTests(ClientAssessmentMixin, TestCase):
    """The service that writes it."""

    def test_records_and_logs(self):
        record_client_assessment(
            self.case,
            self.user,
            assessment="Sounds like the impeller.",
            note="Owner is a mechanical engineer.",
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.client_diagnosis, "Sounds like the impeller.")
        self.assertFalse(self.case.client_gave_no_assessment)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("SVC-01", entry.text)
        self.assertIn("recorded as", entry.text)
        self.assertIn("Owner is a mechanical engineer.", entry.text)

    def test_a_later_theory_is_logged_as_a_change(self):
        # A client who calls back with an idea is ordinary; the
        # technician about to open the engine should see it arrived late.
        record_client_assessment(self.case, self.user, gave_no_assessment=True)
        record_client_assessment(
            self.case, self.user, assessment="My son says it is the fuel pump."
        )
        self.case.refresh_from_db()
        self.assertFalse(self.case.client_gave_no_assessment)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        texts = [
            e.text
            for e in ActivityLog.objects.filter(case=self.case).order_by("pk")
        ]
        self.assertEqual(len(texts), 2)
        self.assertIn("recorded as", texts[0])
        self.assertIn("changed to", texts[1])

    def test_re_recording_the_same_answer_is_silent(self):
        record_client_assessment(self.case, self.user, assessment="Impeller.")
        record_client_assessment(self.case, self.user, assessment="Impeller.")
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertEqual(ActivityLog.objects.filter(case=self.case).count(), 1)

    def test_both_answers_at_once_are_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            record_client_assessment(
                self.case,
                self.user,
                assessment="Impeller.",
                gave_no_assessment=True,
            )
        self.assertIn("not both", str(ctx.exception))

    def test_neither_answer_is_refused(self):
        with self.assertRaises(ValidationError):
            record_client_assessment(self.case, self.user)

    def test_whitespace_is_not_an_assessment(self):
        with self.assertRaises(ValidationError):
            record_client_assessment(self.case, self.user, assessment="   ")

    def test_refused_on_a_case_that_reports_no_fault(self):
        Case = django_apps.get_model("cases", "Case")
        statuses = build_workflow("STORAGE", ["new_report"], [])
        case = Case.objects.create(
            case_type="STORAGE",
            report_channel="PHONE",
            status=statuses["new_report"],
        )
        with self.assertRaises(ValidationError) as ctx:
            record_client_assessment(case, self.user, assessment="x")
        self.assertIn("service question", str(ctx.exception))


class ClientAssessmentAutoAdvanceTests(ClientAssessmentMixin, TestCase):
    """The wizard walks linear edges on guards alone, so this guard is
    what holds the case at ``asset_received``."""

    def test_the_case_waits_at_asset_received_then_moves_on(self):
        self.assertEqual(auto_advance_case(self.case, self.user), [])
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "asset_received")

        record_client_assessment(
            self.case, self.user, assessment="Impeller, I think."
        )
        advanced = auto_advance_case(self.case, self.user)
        self.assertIn("diagnostics", [status.code for status in advanced])


class ClientAssessmentScreenTests(ClientAssessmentMixin, TestCase):
    """It has to be recordable from the case page, not only the shell."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _url(self):
        from django.urls import reverse

        return reverse("backoffice:case-client-assessment", args=[self.case.pk])

    def test_the_card_posts_from_the_case_page(self):
        response = self.client.post(
            self._url(),
            {"client_diagnosis": "Water in the fuel."},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.client_diagnosis, "Water in the fuel.")

    def test_the_no_view_tick_posts_too(self):
        response = self.client.post(
            self._url(), {"gave_no_assessment": "on"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertTrue(self.case.client_gave_no_assessment)

    def test_an_empty_answer_comes_back_as_a_field_error(self):
        response = self.client.post(self._url(), {}, follow=True)
        self.assertContains(response, "invalid-feedback")
        self.case.refresh_from_db()
        self.assertFalse(self.case.client_assessment_recorded)

    def test_the_card_renders_on_a_service_case(self):
        from django.urls import reverse

        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "client-assessment/")
        self.assertContains(response, "not asked")

    def test_the_card_is_absent_on_a_storage_case(self):
        from django.urls import reverse

        Case = django_apps.get_model("cases", "Case")
        statuses = build_workflow("STORAGE", ["new_report"], [])
        case = Case.objects.create(
            case_type="STORAGE",
            report_channel="PHONE",
            status=statuses["new_report"],
        )
        response = self.client.get(
            reverse("backoffice:case-detail", args=[case.pk])
        )
        self.assertNotContains(response, "client-assessment/")


class ClientAssessmentGuideTests(ClientAssessmentMixin, TestCase):
    """Advisory while the client is on the phone, blocking where the
    guard is - the same question at the two moments that matter."""

    def _steps(self, status_code):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self.case.status = self.statuses[status_code]
        self.case.save(update_fields=["status"])
        self.case.refresh_from_db()
        guide = build_guide(self.case, self.user, ctx_for(self.case))
        return {step.key: step for step in guide["steps"]}

    def test_advisory_on_a_fresh_report(self):
        step = self._steps("new_report")["record_client_assessment"]
        self.assertFalse(step.done)
        self.assertFalse(step.blocking)

    def test_blocking_once_the_asset_is_in(self):
        steps = self._steps("asset_received")
        self.assertTrue(steps["record_client_assessment"].blocking)
        self.assertTrue(steps["record_symptoms"].blocking)
        self.assertTrue(steps["record_symptoms"].done)

    def test_the_step_ticks_once_answered(self):
        record_client_assessment(self.case, self.user, gave_no_assessment=True)
        steps = self._steps("asset_received")
        self.assertTrue(steps["record_client_assessment"].done)


class ReportLocationMixin:
    """A SERVICE case with an agreed intake but no location on file."""

    def setUp(self):
        super().setUp()
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p5", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            # §7.1: settled, so the guard's later checks are reachable.
            contact_role=ContactRole.REPORTER,
        )

    def book_intake(self):
        Appointment = django_apps.get_model("scheduling", "Appointment")
        return Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.INTAKE,
            scheduled_for=timezone.now() + timedelta(days=2),
        )


class ReportLocationGuardTests(ReportLocationMixin, TestCase):
    """§7.3: *date, time, location and reporting channel.*

    The date is ``reported_at`` and the channel is ``report_channel``,
    both set by construction. The location was written by both creation
    paths and read only for display - the case-file card, the storage
    intake sheet, the staff notification - so a report could reach the
    scheduling conversation with nobody knowing where the asset was.
    """

    def test_the_intake_cannot_be_scheduled_without_it(self):
        self.book_intake()
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("where the asset is", str(ctx.exception))

    def test_it_is_asked_for_before_the_delivery_method(self):
        # The delivery method is a function of the location: you cannot
        # agree who collects an asset from where until somebody says where.
        self.book_intake()
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("where the asset is", str(ctx.exception))

    def test_the_missing_appointment_still_comes_first(self):
        record_report_location(self.case, "Private yard, Riverside", self.user)
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("intake appointment", str(ctx.exception))

    def test_an_on_site_case_is_asked_too(self):
        # It matters more there, not less: it is the address the
        # technician drives to.
        record_work_location_consult(self.case, WorkLocation.ON_SITE, self.user)
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.ON_SITE_VISIT,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("where the asset is", str(ctx.exception))

    def test_all_three_together_schedule_the_intake(self):
        self.book_intake()
        record_report_location(self.case, "Customer site", self.user)
        record_delivery_method(
            self.case, DeliveryMethod.SERVICE_PICKUP, self.user
        )
        transition_case(self.case, "intake_scheduled", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "intake_scheduled")


class RecordReportLocationServiceTests(ReportLocationMixin, TestCase):
    """The service that writes it."""

    def test_records_and_logs(self):
        record_report_location(
            self.case, "Customer site", self.user, note="Bay C12."
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.location, "Customer site")
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("§7.3", entry.text)
        self.assertIn("recorded as", entry.text)
        self.assertIn("Bay C12.", entry.text)

    def test_a_move_is_logged_as_a_change(self):
        # An asset moves, and a report taken as one site that turns out
        # to be a private yard changes who drives where.
        record_report_location(self.case, "Customer site", self.user)
        record_report_location(self.case, "Private yard, Riverside", self.user)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        texts = [
            e.text
            for e in ActivityLog.objects.filter(case=self.case).order_by("pk")
        ]
        self.assertEqual(len(texts), 2)
        self.assertIn("recorded as", texts[0])
        self.assertIn("changed from", texts[1])

    def test_re_recording_the_same_place_is_silent(self):
        record_report_location(self.case, "Customer site", self.user)
        record_report_location(self.case, "Customer site", self.user)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertEqual(ActivityLog.objects.filter(case=self.case).count(), 1)

    def test_whitespace_is_not_a_location(self):
        with self.assertRaises(ValidationError):
            record_report_location(self.case, "   ", self.user)


class ReportLocationScreenTests(ReportLocationMixin, TestCase):
    """It has to be correctable from the case page, not only the shell."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _url(self):
        from django.urls import reverse

        return reverse("backoffice:case-report-location", args=[self.case.pk])

    def test_the_card_posts_from_the_case_page(self):
        response = self.client.post(
            self._url(), {"location": "Private yard, Riverside"}, follow=True
        )
        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.location, "Private yard, Riverside")

    def test_an_empty_location_comes_back_as_a_field_error(self):
        response = self.client.post(self._url(), {"location": ""}, follow=True)
        self.assertContains(response, "invalid-feedback")
        self.case.refresh_from_db()
        self.assertEqual(self.case.location, "")

    def test_the_card_flags_a_missing_location(self):
        from django.urls import reverse

        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "report-location/")
        self.assertContains(response, "not recorded")


class ReportLocationGuideTests(ReportLocationMixin, TestCase):
    """The wizard mirrors the guard rather than contradicting it."""

    def _step(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self.case.refresh_from_db()
        guide = build_guide(self.case, self.user, ctx_for(self.case))
        for step in guide["steps"]:
            if step.key == "record_report_location":
                return step
        return None

    def test_the_step_is_listed_and_blocking_until_recorded(self):
        step = self._step()
        self.assertIsNotNone(step)
        self.assertFalse(step.done)
        self.assertTrue(step.blocking)

    def test_the_step_ticks_once_recorded(self):
        record_report_location(self.case, "Customer site", self.user)
        self.assertTrue(self._step().done)


class ContactPersonMixin:
    """A SERVICE case reported by somebody, with no contact designated."""

    def setUp(self):
        super().setUp()
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p6", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            # §7.1's first role: whoever called it in.
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            raw_email="mark@example.com",
            location="Customer site",
        )

    def book_intake(self):
        Appointment = django_apps.get_model("scheduling", "Appointment")
        return Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.INTAKE,
            scheduled_for=timezone.now() + timedelta(days=2),
        )

    def link_owner(self, **overrides):
        """§7.1's second role: the owner, matched at triage."""
        Client = django_apps.get_model("clients", "Client")
        fields = {
            "name": "Anna Adams",
            "phone": "+44 68 999 888",
            "email": "ana@example.com",
        }
        fields.update(overrides)
        client = Client.objects.create(**fields)
        self.case.client = client
        self.case.save(update_fields=["client"])
        return client


class ContactPersonGuardTests(ContactPersonMixin, TestCase):
    """§7.1: *reporter, owner and contact person* - three roles.

    The application had homes for two of them and none at all for the
    third. §8 describes the status this guard protects as "New report:
    contact received, appointment not yet confirmed", so the contact is
    precisely what a case is meant to have settled before it has a date.
    """

    def test_the_intake_cannot_be_scheduled_without_a_contact(self):
        self.book_intake()
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("who we contact", str(ctx.exception))

    def test_it_is_asked_before_the_location_and_the_method(self):
        # The other two data of that conversation are things you learn
        # from whoever this names, so this refusal comes first.
        self.case.location = ""
        self.case.save(update_fields=["location"])
        self.book_intake()
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("who we contact", str(ctx.exception))
        self.assertNotIn("where the asset is", str(ctx.exception))

    def test_the_missing_appointment_is_still_reported_first(self):
        # Phase 1's choice, preserved: the status is named after the
        # date, so an empty case still meets that message first.
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("intake appointment", str(ctx.exception))

    def test_naming_the_reporter_opens_the_gate(self):
        # The common case: the person who called is the person to call,
        # and one click says so.
        self.book_intake()
        record_delivery_method(
            self.case, DeliveryMethod.CLIENT_SHIPS, self.user
        )
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        transition_case(self.case, "intake_scheduled", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "intake_scheduled")

    def test_an_on_site_case_is_asked_too(self):
        record_work_location_consult(self.case, WorkLocation.ON_SITE, self.user)
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.ON_SITE_VISIT,
            scheduled_for=timezone.now() + timedelta(days=2),
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("who we contact", str(ctx.exception))


class RecordContactPersonServiceTests(ContactPersonMixin, TestCase):
    """The service that writes the designation."""

    def test_records_and_logs(self):
        record_contact_person(
            self.case, self.user, role=ContactRole.REPORTER, note="Skipper."
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_role, ContactRole.REPORTER)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("§7.1", entry.text)
        self.assertIn("recorded as", entry.text)
        self.assertIn("John Smith", entry.text)
        self.assertIn("Skipper.", entry.text)

    def test_a_re_designation_is_logged_as_a_change(self):
        # A skipper leaves, an owner comes back from abroad, and whoever
        # is about to ring the old number has to see it happened.
        self.link_owner()
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        texts = [
            e.text
            for e in ActivityLog.objects.filter(case=self.case).order_by("pk")
        ]
        self.assertEqual(len(texts), 2)
        self.assertIn("recorded as", texts[0])
        self.assertIn("changed to", texts[1])
        self.assertIn("Anna Adams", texts[1])

    def test_re_designating_the_same_person_is_silent(self):
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertEqual(ActivityLog.objects.filter(case=self.case).count(), 1)

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(ValidationError):
            record_contact_person(self.case, self.user, role="SKIPPER")

    def test_someone_else_needs_a_name(self):
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(
                self.case, self.user, role=ContactRole.OTHER, phone="+44 69 1"
            )
        self.assertIn("Name the person", str(ctx.exception))

    def test_someone_else_needs_a_way_to_be_reached(self):
        # A contact person nobody can reach is not a contact person.
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(
                self.case, self.user, role=ContactRole.OTHER, name="Peter"
            )
        self.assertIn("phone number or an e-mail", str(ctx.exception))

    def test_a_representative_carries_their_own_details(self):
        record_contact_person(
            self.case,
            self.user,
            role=ContactRole.REPRESENTATIVE,
            name="Helen Hill",
            email="helen@example.com",
        )
        self.case.refresh_from_db()
        self.assertEqual(
            self.case.contact_details(),
            ("Helen Hill", "", "helen@example.com"),
        )

    def test_the_owner_cannot_be_the_contact_before_triage(self):
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.assertIn("no client linked", str(ctx.exception))

    def test_an_unreachable_owner_is_refused(self):
        # Otherwise the designation would silently mean "send nothing".
        self.link_owner(phone="", email="")
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.assertIn("neither a phone number nor an e-mail", str(ctx.exception))

    def test_an_anonymous_report_cannot_designate_its_reporter(self):
        self.case.raw_contact_name = ""
        self.case.save(update_fields=["raw_contact_name"])
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        self.assertIn("does not say who made it", str(ctx.exception))

    def test_a_reporter_with_no_number_or_address_is_refused(self):
        self.case.raw_phone = ""
        self.case.raw_email = ""
        self.case.save(update_fields=["raw_phone", "raw_email"])
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        self.assertIn("neither a phone number nor an", str(ctx.exception))

    def test_switching_away_from_someone_else_clears_their_details(self):
        # A superseded number left in the row would sit there looking
        # like current information.
        record_contact_person(
            self.case,
            self.user,
            role=ContactRole.OTHER,
            name="Peter Price",
            phone="+44 69 555 444",
        )
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_name, "")
        self.assertEqual(self.case.contact_phone, "")


class ContactDetailsResolverTests(ContactPersonMixin, TestCase):
    """``contact_details()`` is the one place that answers "who do we
    write to", so each designation is checked against what it resolves
    to - including the undesignated case, which must keep behaving as
    the application did before §7.1 was asked."""

    def test_undesignated_falls_back_to_the_reporter_then_the_client(self):
        self.link_owner()
        self.assertEqual(
            self.case.contact_details(),
            ("John Smith", "+44 67 111 222", "mark@example.com"),
        )

    def test_undesignated_uses_the_client_when_the_report_is_bare(self):
        client = self.link_owner()
        self.case.raw_contact_name = ""
        self.case.raw_phone = ""
        self.case.raw_email = ""
        self.assertEqual(
            self.case.contact_details(), (client.name, client.phone, client.email)
        )

    def test_the_owner_designation_overrides_the_reporter(self):
        # The point of the whole phase: after triage, the owner's details
        # win over the details of whoever happened to file the report.
        client = self.link_owner()
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.case.refresh_from_db()
        self.assertEqual(
            self.case.contact_details(), (client.name, client.phone, client.email)
        )

    def test_the_reporter_designation_is_not_the_same_as_no_answer(self):
        # They resolve identically on this case; the difference is that
        # one of them was chosen, and the guard can tell.
        self.assertFalse(self.case.contact_person_recorded)
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        self.case.refresh_from_db()
        self.assertTrue(self.case.contact_person_recorded)
        self.assertEqual(
            self.case.contact_details(),
            ("John Smith", "+44 67 111 222", "mark@example.com"),
        )


class ContactPersonScreenTests(ContactPersonMixin, TestCase):
    """It has to be designable from the case page."""

    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _url(self):
        from django.urls import reverse

        return reverse("backoffice:case-contact-person", args=[self.case.pk])

    def test_the_card_posts_from_the_case_page(self):
        response = self.client.post(
            self._url(),
            {"role": ContactRole.REPORTER, "name": "", "phone": "", "email": ""},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_role, ContactRole.REPORTER)

    def test_someone_else_without_a_number_comes_back_as_a_field_error(self):
        response = self.client.post(
            self._url(),
            {"role": ContactRole.OTHER, "name": "Peter", "phone": "", "email": ""},
            follow=True,
        )
        self.assertContains(response, "invalid-feedback")
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_role, "")

    def test_the_card_flags_a_case_with_nobody_designated(self):
        from django.urls import reverse

        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "contact-person/")
        self.assertContains(response, "nobody designated")

    def test_the_card_is_offered_on_a_storage_case_too(self):
        # §5.2 records "ownership, contact" for storage as well;
        # only the SERVICE gate enforces it, because intake_scheduled is
        # a SERVICE status.
        from django.urls import reverse

        Case = django_apps.get_model("cases", "Case")
        statuses = build_workflow("STORAGE", ["inquiry"])
        storage = Case.objects.create(
            case_type="STORAGE",
            report_channel="PHONE",
            status=statuses["inquiry"],
            raw_contact_name="John Smith",
        )
        response = self.client.get(
            reverse("backoffice:case-detail", args=[storage.pk])
        )
        self.assertContains(response, "contact-person/")


class ContactPersonGuideTests(ContactPersonMixin, TestCase):
    """The wizard mirrors the guard rather than contradicting it."""

    def _step(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self.case.refresh_from_db()
        guide = build_guide(self.case, self.user, ctx_for(self.case))
        for step in guide["steps"]:
            if step.key == "record_contact_person":
                return step
        return None

    def test_the_step_is_listed_and_blocking_until_designated(self):
        step = self._step()
        self.assertIsNotNone(step)
        self.assertFalse(step.done)
        self.assertTrue(step.blocking)

    def test_the_step_ticks_once_designated(self):
        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        self.assertTrue(self._step().done)


class OnSiteAssetIdentityTests(TestCase):
    """§7.2 phase-4 follow-up: the gate lived in ``sign_intake_record``,
    and an SVC-03 case never signs one.

    "Asset data" are obligatory data of the *ticket*. Attaching the
    check to the receipt meant it went unasked for exactly the jobs where
    nobody stands next to the asset with a form.
    """

    def setUp(self):
        from apps.clients.tests import identified_asset

        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p7a", password="demo12345", is_staff=True, role="MANAGER"
        )
        self.tech = User.objects.create_user(
            username="s7p7at", password="demo12345", is_staff=True, role="TECHNICIAN"
        )
        self.statuses = build_service_workflow()
        self.asset = identified_asset()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            contact_role=ContactRole.REPORTER,
            location="Customer site",
            client=self.asset.client,
            asset=self.asset,
        )

    def _reach_intake_scheduled(self):
        record_work_location_consult(self.case, WorkLocation.ON_SITE, self.user)
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.ON_SITE_VISIT,
            scheduled_for=timezone.now() + timedelta(days=1),
        )
        transition_case(self.case, "intake_scheduled", self.user)

    def _satisfy_the_other_on_site_requirements(self):
        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file
        from apps.service.services import (
            attach_client_approval,
            create_work_order,
            finalize_work_order,
            sign_work_order,
        )

        work_order = create_work_order(self.case, self.user)
        finalize_work_order(work_order, self.user)
        attach_client_approval(
            work_order, SimpleUploadedFile("approval.pdf", b"x"), self.user
        )
        sign_work_order(work_order, self.tech)
        attach_file(
            self.case,
            SimpleUploadedFile("visit.jpg", b"x"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )

    def test_an_on_site_case_is_refused_with_a_half_identified_asset(self):
        self._reach_intake_scheduled()
        self._satisfy_the_other_on_site_requirements()
        self.asset.asset_type = ""
        self.asset.manufacturer = ""
        self.asset.save(update_fields=["asset_type", "manufacturer"])
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "asset_received", self.user)
        self.assertIn("§7.2", str(ctx.exception))
        self.assertIn("identification", str(ctx.exception))

    def test_an_on_site_case_with_no_asset_at_all_is_refused(self):
        self._reach_intake_scheduled()
        self._satisfy_the_other_on_site_requirements()
        self.case.asset = None
        self.case.save(update_fields=["asset"])
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "asset_received", self.user)
        self.assertIn("which asset this is", str(ctx.exception))

    def test_a_fully_identified_on_site_case_walks_on(self):
        self._reach_intake_scheduled()
        self._satisfy_the_other_on_site_requirements()
        transition_case(self.case, "asset_received", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "asset_received")

    def test_the_fuel_gauge_is_deliberately_not_asked_of_an_on_site_case(self):
        # §7.6 is a property of the intake record - "intake record with a list
        # of ... fuel ..." - and a case that writes no intake record has no
        # such list to leave blank. The asymmetry with §7.2 is the point.
        self._reach_intake_scheduled()
        self._satisfy_the_other_on_site_requirements()
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        self.assertFalse(IntakeRecord.objects.filter(case=self.case).exists())
        transition_case(self.case, "asset_received", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "asset_received")

    def test_the_guide_asks_an_on_site_case_for_the_identification(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        self._reach_intake_scheduled()
        self.asset.asset_type = ""
        self.asset.save(update_fields=["asset_type"])
        self.case.refresh_from_db()
        guide = build_guide(self.case, self.user, ctx_for(self.case))
        keys = [s.key for s in guide["steps"]]
        # The step used to carry applies=needs_asset_intake, so the
        # wizard agreed with a rule that was not being enforced.
        self.assertIn("identify_asset", keys)


class StaleDeliveryMethodTests(TestCase):
    """SVC-04 phase-1 follow-up: an on-site consult supersedes a delivery
    method agreed before it."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p7b", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            raw_contact_name="John Smith",
            location="Customer site",
        )

    def test_an_on_site_consult_retires_the_delivery_method(self):
        record_delivery_method(self.case, DeliveryMethod.SERVICE_PICKUP, self.user)
        record_work_location_consult(self.case, WorkLocation.ON_SITE, self.user)
        self.case.refresh_from_db()
        # The guard was satisfied either way; what was wrong is that the
        # column said "we collect it" while the consult said we do not.
        self.assertEqual(self.case.delivery_method, "")
        self.assertTrue(self.case.works_on_site)

    def test_the_retirement_is_logged_with_what_it_replaced(self):
        record_delivery_method(self.case, DeliveryMethod.SERVICE_PICKUP, self.user)
        record_work_location_consult(self.case, WorkLocation.IN_FIELD, self.user)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.filter(
            case=self.case, text__contains="cleared"
        ).latest("pk")
        self.assertIn("SVC-04", entry.text)
        self.assertIn("We collect it", entry.text)

    def test_a_yard_consult_leaves_the_method_alone(self):
        record_delivery_method(self.case, DeliveryMethod.SERVICE_PICKUP, self.user)
        record_work_location_consult(
            self.case, WorkLocation.SERVICE_CENTER, self.user
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.delivery_method, DeliveryMethod.SERVICE_PICKUP)


class ContactDesignationHardeningTests(TestCase):
    """§7.1 phase-6 follow-up: three ways the designation could stop
    meaning what the screen said."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        Client = django_apps.get_model("clients", "Client")
        self.user = User.objects.create_user(
            username="s7p7c", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PORTAL",
            status=self.statuses["new_report"],
            raw_contact_name="Site Office",
            raw_phone="+44 32 000 000",
            raw_email="office@site.example.com",
        )
        self.owner = Client.objects.create(
            name="Anna Adams", phone="+44 68 999 888", email="ana@example.com"
        )
        self.case.client = self.owner
        self.case.save(update_fields=["client"])

    def test_the_service_refuses_an_unparseable_address(self):
        # The form checked the shape and the service did not - a rule
        # enforced by one form is enforced only for people who open it.
        with self.assertRaises(ValidationError) as ctx:
            record_contact_person(
                self.case,
                self.user,
                role=ContactRole.OTHER,
                name="Peter",
                email="not-an-address",
            )
        self.assertIn("not a valid e-mail", str(ctx.exception))
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_role, "")

    def test_a_valid_address_still_goes_through(self):
        record_contact_person(
            self.case,
            self.user,
            role=ContactRole.OTHER,
            name="Peter",
            email="peter@example.com",
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_email, "peter@example.com")

    def test_owner_with_no_client_resolves_to_nobody_not_to_the_reporter(self):
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.case.client = None
        self.case.save(update_fields=["client"])
        # Guarding the OWNER branch on "client is not None" dropped this
        # into the guessing branch, which answered with the reporter
        # while the card still said OWNER.
        self.assertEqual(self.case.contact_details(), ("", "", ""))

    def test_relinking_to_an_unreachable_client_retires_the_designation(self):
        from apps.clients.services import link_case

        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        Client = django_apps.get_model("clients", "Client")
        silent = Client.objects.create(name="New Owner")  # no phone, no email
        link_case(self.case, silent, user=self.user)
        self.case.refresh_from_db()
        # Retired rather than left standing: a designation that resolves
        # to nothing sends nothing, and the guard should ask again.
        self.assertEqual(self.case.contact_role, "")
        self.assertFalse(self.case.contact_person_recorded)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="designation is retired"
            ).exists()
        )

    def test_relinking_to_a_reachable_client_keeps_the_designation(self):
        from apps.clients.services import link_case

        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        Client = django_apps.get_model("clients", "Client")
        other = Client.objects.create(name="Drugi", email="drugi@example.com")
        link_case(self.case, other, user=self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_role, ContactRole.OWNER)
        self.assertEqual(self.case.contact_details()[2], "drugi@example.com")

    def test_a_reporter_designation_is_untouched_by_a_relink(self):
        from apps.clients.services import link_case

        record_contact_person(self.case, self.user, role=ContactRole.REPORTER)
        Client = django_apps.get_model("clients", "Client")
        silent = Client.objects.create(name="New Owner")
        link_case(self.case, silent, user=self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_role, ContactRole.REPORTER)


class TriageKeepsTheAssetTests(TestCase):
    """``link_case`` overwrote ``case.asset`` unconditionally, so a
    re-link that named no asset detached one §7.2 had identified."""

    def setUp(self):
        from apps.clients.tests import identified_asset

        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p7d", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.asset = identified_asset()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            raw_contact_name="Mark",
            client=self.asset.client,
            asset=self.asset,
        )

    def test_relinking_the_same_client_keeps_the_asset(self):
        from apps.clients.services import link_case

        link_case(self.case, self.asset.client, user=self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset_id, self.asset.pk)

    def test_relinking_a_different_client_detaches_their_asset(self):
        # Still correct to drop it: it is not this client's asset.
        from apps.clients.services import link_case

        Client = django_apps.get_model("clients", "Client")
        other = Client.objects.create(name="Drugi", phone="+44 69 1")
        link_case(self.case, other, user=self.user)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.asset)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="belongs to another client"
            ).exists()
        )

    def test_an_explicitly_named_asset_still_wins(self):
        from apps.clients.services import link_case
        from apps.clients.tests import identified_asset

        second = identified_asset(client=self.asset.client, name="Second Asset")
        link_case(self.case, self.asset.client, asset=second, user=self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset_id, second.pk)


class AssetIdentityBelongsToTheStatusTests(TestCase):
    """Second-pass follow-up. The §7.2 check was first written into the
    on-site branch of ``_guard_asset_received``, because that is where
    the symptom was found; the rule is "a ticket names its asset", and
    scoping it to one branch left the other open.

    An in-yard case IS covered by ``sign_intake_record`` - at signing
    time. ``link_case`` can then move the case to another client, which
    correctly drops an asset belonging to the previous one, and the
    in-yard branch asks only whether a signed record exists.
    """

    def setUp(self):
        from apps.clients.tests import identified_asset

        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p8a", password="demo12345", is_staff=True, role="MANAGER"
        )
        self.tech = User.objects.create_user(
            username="s7p8t", password="demo12345", is_staff=True, role="TECHNICIAN"
        )
        self.statuses = build_service_workflow()
        self.asset = identified_asset()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["intake_scheduled"],
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            contact_role=ContactRole.REPORTER,
            location="Customer site",
            client=self.asset.client,
            asset=self.asset,
        )

    def _sign_everything_in_yard(self):
        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file
        from apps.intake.services import create_intake_record, sign_intake_record
        from apps.service.services import (
            attach_client_approval,
            create_work_order,
            finalize_work_order,
            sign_work_order,
        )

        work_order = create_work_order(self.case, self.user)
        finalize_work_order(work_order, self.user)
        attach_client_approval(
            work_order, SimpleUploadedFile("work-order.pdf", b"x"), self.user
        )
        sign_work_order(work_order, self.tech)
        attach_file(
            self.case,
            SimpleUploadedFile("dolazak.jpg", b"x"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )
        record = create_intake_record(
            self.case,
            self.user,
            mount_status="OFF_MOUNT",
            cleanliness_status="CLEAN",
            storage_location="Hall 1",
            damage_note="none",
            energy_level="HALF",
        )
        record.no_items_received = True
        record.save(update_fields=["no_items_received"])
        sign_intake_record(record, self.user)

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_a_signed_intake_no_longer_covers_a_lost_asset(self):
        from apps.clients.services import link_case

        self._sign_everything_in_yard()
        Client = django_apps.get_model("clients", "Client")
        other = Client.objects.create(name="Drugi", phone="+44 69 111 222")
        link_case(self.case, other, user=self.user)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.asset)

        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "asset_received", self.user)
        self.assertIn("which asset this is", str(ctx.exception))

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_the_ordinary_in_yard_case_still_walks_on(self):
        self._sign_everything_in_yard()
        transition_case(self.case, "asset_received", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "asset_received")

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp())
    def test_a_half_identified_asset_is_refused_in_the_yard_too(self):
        self._sign_everything_in_yard()
        # Identified when the receipt was signed, blanked afterwards.
        self.asset.manufacturer = ""
        self.asset.save(update_fields=["manufacturer"])
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "asset_received", self.user)
        self.assertIn("§7.2", str(ctx.exception))


class ContactMustStillResolveTests(TestCase):
    """Second-pass follow-up: ``contact_person_recorded`` asked whether a
    role had been CHOSEN, not whether it still reaches anybody."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        Client = django_apps.get_model("clients", "Client")
        self.user = User.objects.create_user(
            username="s7p8b", password="demo12345", is_staff=True
        )
        self.statuses = build_service_workflow()
        self.owner = Client.objects.create(
            name="Anna Adams", phone="+44 68 999 888", email="ana@example.com"
        )
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["new_report"],
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            location="Customer site",
            client=self.owner,
        )

    def test_a_designation_that_reaches_nobody_does_not_count(self):
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.assertTrue(self.case.contact_person_recorded)
        # The client the designation points at is cleared underneath it.
        self.case.client = None
        self.case.save(update_fields=["client"])
        self.assertFalse(self.case.contact_person_recorded)

    def test_a_name_without_a_number_is_not_a_contact(self):
        # The same bar record_contact_person applies at designation time,
        # now applied to whatever the designation resolves to today.
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        self.owner.phone = ""
        self.owner.email = ""
        self.owner.save(update_fields=["phone", "email"])
        self.case.refresh_from_db()
        self.assertEqual(self.case.contact_details()[0], "Anna Adams")
        self.assertFalse(self.case.contact_person_recorded)

    def test_the_guard_refuses_such_a_case(self):
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.INTAKE,
            scheduled_for=timezone.now() + timedelta(days=1),
        )
        record_contact_person(self.case, self.user, role=ContactRole.OWNER)
        record_delivery_method(self.case, DeliveryMethod.CLIENT_SHIPS, self.user)
        self.owner.phone = ""
        self.owner.email = ""
        self.owner.save(update_fields=["phone", "email"])
        self.case.refresh_from_db()
        with self.assertRaises(GuardCheckFailed) as ctx:
            transition_case(self.case, "intake_scheduled", self.user)
        self.assertIn("who we contact", str(ctx.exception))

    def test_an_undesignated_case_is_still_simply_undesignated(self):
        self.assertFalse(self.case.contact_person_recorded)


class AssetIdentityCardWithoutAAssetTests(TestCase):
    """Second-pass follow-up: widening the ``identify_asset`` step to
    on-site cases exposed a dead end the old ``applies`` had hidden - the
    card sat inside ``{% if case.asset %}``, so the wizard pointed at a
    section the page did not draw."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        Case = django_apps.get_model("cases", "Case")
        self.user = User.objects.create_user(
            username="s7p8c", password="demo12345", is_staff=True, role="MANAGER"
        )
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type="SERVICE",
            report_channel="PHONE",
            status=self.statuses["intake_scheduled"],
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            contact_role=ContactRole.REPORTER,
            location="Customer site",
        )
        self.client.force_login(self.user)

    def _detail(self):
        from django.urls import reverse

        return self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )

    def test_the_step_has_a_section_to_point_at(self):
        from apps.backoffice.guide import build_guide
        from apps.backoffice.tests_guide import ctx_for

        guide = build_guide(self.case, self.user, ctx_for(self.case))
        step = next(
            (s for s in guide["steps"] if s.key == "identify_asset"), None
        )
        self.assertIsNotNone(step)
        self.assertContains(self._detail(), 'id="acc-asset-identity"')

    def test_the_card_says_why_and_names_triage(self):
        self.assertContains(self._detail(), "No asset linked yet")

    def test_an_untriaged_case_is_offered_the_triage_button(self):
        from django.urls import reverse

        self.assertContains(
            self._detail(), reverse("backoffice:case-triage", args=[self.case.pk])
        )

    def test_a_triaged_case_is_not(self):
        # Same shape as the STORAGE "acc-asset-link" card: the
        # button is for a case still awaiting triage, and a case whose
        # client is already linked is not one.
        from django.urls import reverse

        Client = django_apps.get_model("clients", "Client")
        self.case.client = Client.objects.create(
            name="Linked", phone="+44 68 1"
        )
        self.case.save(update_fields=["client"])
        response = self._detail()
        self.assertContains(response, "No asset linked yet")
        self.assertNotContains(
            response, reverse("backoffice:case-triage", args=[self.case.pk])
        )
