"""Tests for the scheduling app (SVC-04 appointment booking)."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from apps.cases.models import (
    ActivityLog,
    Case,
    CaseType,
    ContactRole,
    ReportChannel,
    Status,
)

from .models import Appointment
from .services import book_appointment
from .workinghours import next_working_slot


class SchedulingSmokeTest(SimpleTestCase):
    def test_models_import(self):
        from . import models

        self.assertTrue(hasattr(models, "Appointment"))


class BookAppointmentTests(TestCase):
    """SVC-04: appointments are recorded on the case, forward in time."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="fd", password="demo12345")
        # Not a workflow code: build_service_workflow() in the guard test
        # below creates the real SERVICE statuses without clashing.
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="fixture_status",
            name="n",
            client_visible_name="n",
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
            location="Customer site",
        )
        # A fixed working-hours slot, not a bare "tomorrow": the yard is
        # shut on Sundays and after 17:00, so an arbitrary offset would
        # make the suite fail depending on the day it runs (SVC-04).
        self.tomorrow = next_working_slot()

    def test_books_appointment_and_logs(self):
        appointment = book_appointment(
            self.case,
            Appointment.Kind.INTAKE,
            self.tomorrow,
            self.user,
            note="Morning slot.",
        )
        self.assertEqual(appointment.case, self.case)
        self.assertEqual(appointment.note, "Morning slot.")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertEqual(entry.author, self.user)
        self.assertIn("SVC-04", entry.text)

    def test_refuses_past_datetime(self):
        with self.assertRaises(ValidationError):
            book_appointment(
                self.case,
                Appointment.Kind.INTAKE,
                timezone.now() - timedelta(hours=1),
                self.user,
            )
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_refuses_missing_datetime(self):
        with self.assertRaises(ValidationError):
            book_appointment(self.case, Appointment.Kind.INTAKE, None, self.user)

    def test_refuses_unknown_kind(self):
        with self.assertRaises(ValidationError) as ctx:
            book_appointment(self.case, "LUNCH", self.tomorrow, self.user)
        self.assertIn("INTAKE", str(ctx.exception))
        self.assertEqual(Appointment.objects.count(), 0)

    def test_refuses_time_after_closing(self):
        """SVC-04: 18:00 on a weekday is past the 17:00 close."""
        with self.assertRaises(ValidationError) as ctx:
            book_appointment(
                self.case,
                Appointment.Kind.INTAKE,
                self.tomorrow.replace(hour=18, minute=0),
                self.user,
            )
        self.assertIn("working hours", str(ctx.exception))
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_refuses_sunday(self):
        """SVC-04: Sunday is a non-working day at any hour."""
        sunday = self.tomorrow
        while sunday.weekday() != 6:
            sunday += timedelta(days=1)
        with self.assertRaises(ValidationError):
            book_appointment(
                self.case, Appointment.Kind.INTAKE, sunday, self.user
            )
        self.assertEqual(Appointment.objects.count(), 0)

    def test_saturday_closes_at_14(self):
        """SVC-04: Saturday runs 08:00-14:00, so 15:00 is refused and
        13:00 is not."""
        saturday = self.tomorrow
        while saturday.weekday() != 5:
            saturday += timedelta(days=1)
        with self.assertRaises(ValidationError):
            book_appointment(
                self.case,
                Appointment.Kind.INTAKE,
                saturday.replace(hour=15, minute=0),
                self.user,
            )
        appointment = book_appointment(
            self.case,
            Appointment.Kind.INTAKE,
            saturday.replace(hour=13, minute=0),
            self.user,
        )
        self.assertEqual(Appointment.objects.count(), 1)
        self.assertEqual(timezone.localtime(appointment.scheduled_for).hour, 13)

    def test_edges_of_the_window_are_bookable(self):
        """"Nine to five" includes both ends - 17:00 is the last slot.

        Anchored a week out on a Mon-Fri: rewriting the hour of a slot
        that is only hours away could land in the past and be refused by
        the *other* rule, depending on when the suite runs.
        """
        day = next_working_slot(timezone.now() + timedelta(days=7))
        while day.weekday() > 4:
            day = next_working_slot(day)
        for hour in (9, 17):
            with self.subTest(hour=hour):
                self.assertIsNotNone(
                    book_appointment(
                        self.case,
                        Appointment.Kind.INTAKE,
                        day.replace(hour=hour, minute=0),
                        self.user,
                    )
                )

    def test_booked_intake_appointment_satisfies_svc04_guard(self):
        from apps.cases.services import transition_case
        from apps.cases.tests import build_service_workflow

        statuses = build_service_workflow()
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=statuses["new_report"],
            location="Customer site",
            # §7.1: the same guard asks who we contact about the case.
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
            contact_role=ContactRole.REPORTER,
        )
        book_appointment(case, Appointment.Kind.INTAKE, self.tomorrow, self.user)
        # SVC-04: the date is half of it; the way the asset gets here is
        # the other half, and the same guard now asks for both.
        case.delivery_method = "CLIENT_SHIPS"
        case.save(update_fields=["delivery_method"])
        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                transition_case(case, "intake_scheduled", self.user)
        case.refresh_from_db()
        self.assertEqual(case.status.code, "intake_scheduled")
