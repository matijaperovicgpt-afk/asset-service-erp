"""Tests for the transport app (TRP-03 soft mode validation, TRP-05
standard departure days as configuration data)."""
import tempfile
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.admin.sites import AdminSite
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from apps.clients.tests import link_identified_asset

from .admin import TransportOrderAdmin, TransportOrderForm
from .models import (
    DeliveryConfirmation,
    LoadingConfirmation,
    StandardDepartureDay,
    TransportOrder,
)
from .services import (
    active_departure_day_choices,
    attach_delivery_protocol,
    create_transport_offer,
    declare_carrier_price,
    ensure_transport_advance,
    has_delivery_protocol,
    mark_delivered,
    recommended_mode,
    record_delivery_confirmation,
    record_loading_confirmation,
    validate_departure_day,
    validate_mode,
)


def build_transport_case(asset_length_m=None):
    """Create a TRANSPORT case, optionally with a client + asset."""
    Status = django_apps.get_model("cases", "Status")
    Case = django_apps.get_model("cases", "Case")
    status = Status.objects.create(
        case_type="TRANSPORT",
        code="new_report",
        name="new_report",
        client_visible_name="new_report",
    )
    asset = None
    if asset_length_m is not None:
        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")
        client = Client.objects.create(name="John Smith")
        asset = Asset.objects.create(
            client=client,
            name="Unit 7",
            asset_type="Generator",
            length_m=Decimal(asset_length_m),
        )
    return Case.objects.create(
        case_type="TRANSPORT",
        report_channel="PHONE",
        status=status,
        asset=asset,
    )


def sign_intake(case, user=None):
    """TRP-06: the signed pre-transport intake record.

    Written straight to the row rather than through ``sign_intake_record``
    because these tests are about what happens *after* the intake record
    exists; SVC-07's photo requirement is pinned in the intake suite.
    """
    IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
    return IntakeRecord.objects.create(
        case=case, received_by=user, signed_at=timezone.now()
    )


def mark_departed(case):
    """Move ``case`` onto ``in_transit``.

    TRP-08 refuses a delivery protocol for an asset that has not left, and
    "has it left" is read from ``Status.sort_order`` - so the fixture needs
    a real ``in_transit`` row, not just any status.
    """
    Status = django_apps.get_model("cases", "Status")
    status, _created = Status.objects.get_or_create(
        case_type="TRANSPORT",
        code="in_transit",
        defaults={
            "name": "in_transit",
            "client_visible_name": "in_transit",
            "sort_order": 50,
        },
    )
    case.status = status
    case.save(update_fields=["status"])
    return case


class ValidateModeTest(TestCase):
    """TRP-03: own vehicle only suitable up to ~10 m - soft warning."""

    def _activity_log(self):
        return django_apps.get_model("cases", "ActivityLog")

    def test_own_vehicle_over_10m_logs_warning(self):
        case = build_transport_case(asset_length_m="11.50")
        order = TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.OWN_VEHICLE,
        )
        warning = validate_mode(order)
        self.assertIsNotNone(warning)
        self.assertIn("11.50", warning)
        self.assertIn("external carrier", warning)
        ActivityLog = self._activity_log()
        entry = ActivityLog.objects.get(case=case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertEqual(entry.text, warning)
        self.assertIn("11.50", entry.text)

    def test_own_vehicle_at_or_under_10m_is_fine(self):
        case = build_transport_case(asset_length_m="10.00")
        order = TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.OWN_VEHICLE,
        )
        self.assertIsNone(validate_mode(order))
        self.assertEqual(self._activity_log().objects.count(), 0)

    def test_no_asset_linked_is_fine(self):
        case = build_transport_case(asset_length_m=None)
        order = TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.OWN_VEHICLE,
        )
        self.assertIsNone(validate_mode(order))
        self.assertEqual(self._activity_log().objects.count(), 0)

    def test_external_carrier_over_10m_is_fine(self):
        case = build_transport_case(asset_length_m="15.00")
        order = TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        self.assertIsNone(validate_mode(order))
        self.assertEqual(self._activity_log().objects.count(), 0)


class TransportOrderAdminTest(TestCase):
    """save_model surfaces the TRP-03 warning via messages.warning."""

    def _save_via_admin(self, order):
        model_admin = TransportOrderAdmin(TransportOrder, AdminSite())
        request = RequestFactory().post("/")
        with patch("apps.transport.admin.messages.warning") as mock_warning:
            model_admin.save_model(request, order, form=None, change=False)
        return mock_warning

    def test_warning_surfaced_for_long_asset(self):
        case = build_transport_case(asset_length_m="12.00")
        order = TransportOrder(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.OWN_VEHICLE,
        )
        mock_warning = self._save_via_admin(order)
        self.assertTrue(TransportOrder.objects.filter(pk=order.pk).exists())
        mock_warning.assert_called_once()
        self.assertIn("external carrier", mock_warning.call_args.args[1])

    def test_no_warning_for_short_asset(self):
        case = build_transport_case(asset_length_m="8.00")
        order = TransportOrder(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.OWN_VEHICLE,
        )
        mock_warning = self._save_via_admin(order)
        self.assertTrue(TransportOrder.objects.filter(pk=order.pk).exists())
        mock_warning.assert_not_called()


class StandardDepartureDayTests(TestCase):
    """TRP-05 / OPEN-04: departure days are configuration, not code."""

    def setUp(self):
        self.tue = StandardDepartureDay.objects.create(
            code="TUE", name="Utorak", weekday=1
        )
        self.sat = StandardDepartureDay.objects.create(
            code="SAT", name="Subota", weekday=5
        )

    def _order(self, **kwargs):
        case = build_transport_case()
        return TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER, **kwargs,
        )

    def _activity_log(self):
        return django_apps.get_model("cases", "ActivityLog")

    def test_choices_list_only_active_days(self):
        self.sat.is_active = False
        self.sat.save()
        self.assertEqual(active_departure_day_choices(), [("TUE", "Utorak")])

    def test_unknown_code_raises_naming_allowed_codes(self):
        order = self._order(standard_departure_day="MON")
        with self.assertRaises(ValidationError) as ctx:
            validate_departure_day(order)
        self.assertIn("TUE", str(ctx.exception))
        self.assertIn("SAT", str(ctx.exception))

    def test_inactive_code_is_refused(self):
        self.sat.is_active = False
        self.sat.save()
        order = self._order(standard_departure_day="SAT")
        with self.assertRaises(ValidationError):
            validate_departure_day(order)

    def test_active_code_passes_silently(self):
        order = self._order(standard_departure_day="TUE")
        self.assertIsNone(validate_departure_day(order))
        self.assertEqual(self._activity_log().objects.count(), 0)

    def test_preferred_date_on_standard_day_is_silent(self):
        order = self._order(preferred_date=date(2026, 8, 4))  # a Tuesday
        self.assertIsNone(validate_departure_day(order))
        self.assertEqual(self._activity_log().objects.count(), 0)

    def test_preferred_date_off_standard_days_warns_without_blocking(self):
        order = self._order(preferred_date=date(2026, 8, 3))  # a Monday
        warning = validate_departure_day(order)
        self.assertIsNotNone(warning)
        self.assertIn("TRP-05", warning)
        entry = self._activity_log().objects.get(case=order.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertEqual(entry.text, warning)

    def test_no_configured_days_means_no_date_warning(self):
        StandardDepartureDay.objects.all().delete()
        order = self._order(preferred_date=date(2026, 8, 3))
        self.assertIsNone(validate_departure_day(order))

    def test_admin_form_choices_come_from_configuration(self):
        form = TransportOrderForm()
        self.assertEqual(
            form.fields["standard_departure_day"].choices,
            [("", "---------"), ("TUE", "Utorak"), ("SAT", "Subota")],
        )


class MarkDeliveredTests(TestCase):
    """TRP-08: delivery recorded once, never transitions the case."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(username="fd", password="demo12345")
        self.case = build_transport_case()
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        # QA T-H4: only an asset that has departed can be delivered.
        Status = django_apps.get_model("cases", "Status")
        self.in_transit = Status.objects.create(
            case_type="TRANSPORT", code="in_transit", name="in_transit",
            client_visible_name="in_transit", sort_order=4,
        )
        self.case.status = self.in_transit
        self.case.save(update_fields=["status"])

    def test_refused_before_the_asset_has_departed(self):
        self.case.status = django_apps.get_model("cases", "Status").objects.get(
            case_type="TRANSPORT", code="new_report"
        )
        self.case.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            mark_delivered(self.order, self.user)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.delivered_at)

    def test_marks_delivered_with_note_and_log(self):
        mark_delivered(self.order, self.user, note="Left at bay 3.")
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)
        self.assertEqual(self.order.delivery_note, "Left at bay 3.")
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("TRP-08", entry.text)
        # Status untouched (moving the case is a separate staff decision).
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "in_transit")

    def test_double_delivery_is_refused(self):
        mark_delivered(self.order, self.user)
        first = self.order.delivered_at
        with self.assertRaises(ValidationError):
            mark_delivered(self.order, self.user)
        self.order.refresh_from_db()
        self.assertEqual(self.order.delivered_at, first)


class RecordLoadingConfirmationTests(TestCase):
    """TRP-07: internal loading confirmation - signature, radio choices,
    logged as an activity entry, upsert not duplicate."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(username="fd", password="demo12345")
        self.case = build_transport_case()
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        # TRP-06: the intake record is made before loading, so a loading
        # confirmation cannot be recorded without one.
        sign_intake(self.case, self.user)

    def test_records_confirmation_signs_and_logs(self):
        confirmation = record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRAILER,
            readiness=LoadingConfirmation.Readiness.READY,
            lifting_equipment_used=True,
            carrier="Carrier Ltd",
            client_notified=True,
            notes="Called the owner.",
        )
        self.assertEqual(
            confirmation.vehicle_type, LoadingConfirmation.VehicleType.TRAILER
        )
        self.assertEqual(
            confirmation.readiness, LoadingConfirmation.Readiness.READY
        )
        self.assertTrue(confirmation.lifting_equipment_used)
        self.assertTrue(confirmation.client_notified)
        # Submitting is the responsible person's sign-off.
        self.assertEqual(confirmation.signed_by, self.user)
        self.assertIsNotNone(confirmation.signed_at)
        # TRP-07 is logged as an activity entry.
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("TRP-07", entry.text)

    def test_second_call_upserts_not_duplicates(self):
        record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRUCK,
            readiness=LoadingConfirmation.Readiness.SECURED,
        )
        # TRP-07: a READY sign-off on an external-carrier job names the
        # carrier who checked the securing with us.
        record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRUCK,
            readiness=LoadingConfirmation.Readiness.READY,
            carrier="Carrier Ltd",
        )
        self.assertEqual(
            LoadingConfirmation.objects.filter(transport_order=self.order).count(),
            1,
        )
        confirmation = self.order.loading_confirmation
        confirmation.refresh_from_db()
        self.assertEqual(
            confirmation.readiness, LoadingConfirmation.Readiness.READY
        )

    def test_refuses_case_without_transport_order(self):
        Case = django_apps.get_model("cases", "Case")
        other = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=self.case.status,
        )
        with self.assertRaises(ValidationError):
            record_loading_confirmation(
                other,
                user=self.user,
                vehicle_type=LoadingConfirmation.VehicleType.TRUCK,
                readiness=LoadingConfirmation.Readiness.READY,
            )


class RecordDeliveryConfirmationTests(TestCase):
    """TRP-08: delivery protocol - mandatory attendee, staff sign-off on
    submit, delivered_at stamped once, logged as an activity entry,
    upsert not duplicate."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(username="fd", password="demo12345")
        self.case = build_transport_case()
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        # TRP-08: a delivery protocol attests to a delivery, so the
        # asset has to have departed first.
        mark_departed(self.case)

    def test_records_confirmation_signs_stamps_and_logs(self):
        confirmation = record_delivery_confirmation(
            self.case,
            user=self.user,
            unloading_done=True,
            put_into_service=True,
            attendee_role=DeliveryConfirmation.AttendeeRole.OWNER,
            attendee_name="Pat Owner",
            attendee_contact="067 123 456",
            notes="Clear weather.",
        )
        self.assertTrue(confirmation.unloading_done)
        self.assertTrue(confirmation.put_into_service)
        self.assertEqual(confirmation.attendee_name, "Pat Owner")
        # Submitting is the staff member's sign-off.
        self.assertEqual(confirmation.signed_by, self.user)
        self.assertIsNotNone(confirmation.signed_at)
        # One submission also records the delivery moment.
        self.order.refresh_from_db()
        self.assertIsNotNone(self.order.delivered_at)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("TRP-08", entry.text)
        self.assertIn("Pat Owner", entry.text)
        # Status untouched (moving the case is a separate staff decision).
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "in_transit")

    def test_existing_delivered_at_is_left_alone(self):
        mark_delivered(self.order, self.user)
        first = self.order.delivered_at
        record_delivery_confirmation(
            self.case,
            user=self.user,
            attendee_name="Pat Owner",
            unloading_done=True,
            put_into_service=True,
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.delivered_at, first)

    def test_representative_role_round_trips(self):
        confirmation = record_delivery_confirmation(
            self.case,
            user=self.user,
            attendee_role=DeliveryConfirmation.AttendeeRole.REPRESENTATIVE,
            attendee_name="Anna Proxy",
            unloading_done=True,
            put_into_service=True,
        )
        confirmation.refresh_from_db()
        self.assertEqual(
            confirmation.attendee_role,
            DeliveryConfirmation.AttendeeRole.REPRESENTATIVE,
        )
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.case)
        self.assertIn("Authorized representative", entry.text)

    def test_blank_attendee_is_refused(self):
        with self.assertRaises(ValidationError):
            record_delivery_confirmation(
                self.case, user=self.user, attendee_name="  "
            )
        self.assertEqual(DeliveryConfirmation.objects.count(), 0)

    def test_second_call_upserts_not_duplicates(self):
        record_delivery_confirmation(
            self.case, user=self.user,
            attendee_name="Pat Owner", unloading_done=True,
            put_into_service=False, notes="Delivered onto the owner's vehicle.",
        )
        # The second call omits ``unloading_done``: the upsert keeps it,
        # and the TRP-08 check reads the record as it will stand.
        record_delivery_confirmation(
            self.case, user=self.user,
            attendee_name="Pat Owner", put_into_service=True,
        )
        self.assertEqual(
            DeliveryConfirmation.objects.filter(transport_order=self.order).count(),
            1,
        )
        confirmation = self.order.delivery_confirmation
        confirmation.refresh_from_db()
        self.assertTrue(confirmation.put_into_service)

    def test_refuses_case_without_transport_order(self):
        Case = django_apps.get_model("cases", "Case")
        other = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=self.case.status,
        )
        with self.assertRaises(ValidationError):
            record_delivery_confirmation(
                other, user=self.user, attendee_name="Pat Owner"
            )


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class DeliveryProtocolAttachmentTests(TestCase):
    """TRP-08: the physically signed protocol scan is a DOCUMENT
    attachment on the confirmation, filed under the case."""

    def setUp(self):
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(username="fd", password="demo12345")
        self.case = build_transport_case()
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        mark_departed(self.case)
        self.confirmation = record_delivery_confirmation(
            self.case,
            user=self.user,
            attendee_name="Pat Owner",
            unloading_done=True,
            put_into_service=True,
        )

    def test_attaches_scan_and_flips_has_protocol(self):
        self.assertFalse(has_delivery_protocol(self.confirmation))
        attachment = attach_delivery_protocol(
            self.confirmation,
            SimpleUploadedFile("record.pdf", b"%PDF-1.4 scan"),
            self.user,
        )
        Attachment = django_apps.get_model("attachments", "Attachment")
        self.assertEqual(attachment.kind, Attachment.Kind.DOCUMENT)
        self.assertEqual(attachment.stage, Attachment.Stage.HANDOVER)
        self.assertEqual(attachment.original_name, "record.pdf")
        # The case_id property files the scan under the case, not "unlinked".
        self.assertIn(f"cases/{self.case.pk}/handover/", attachment.file.name)
        self.assertTrue(has_delivery_protocol(self.confirmation))
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="TRP-08"
            ).exists()
        )


class DeliveredGuardTests(TestCase):
    """TRP-08: `in_transit -> delivered` is refused until the delivery
    protocol is recorded."""

    def setUp(self):
        from apps.cases.tests import build_workflow

        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(username="fd", password="demo12345")
        Case = django_apps.get_model("cases", "Case")
        self.statuses = build_workflow("TRANSPORT", ["in_transit", "delivered"])
        self.case = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=self.statuses["in_transit"],
        )
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )

    def test_transition_blocked_without_confirmation_allowed_with(self):
        from apps.cases.exceptions import GuardCheckFailed
        from apps.cases.services import transition_case

        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(GuardCheckFailed) as ctx:
                    transition_case(self.case, "delivered", self.user)
                self.assertIn("TRP-08", str(ctx.exception))

                record_delivery_confirmation(
                    self.case,
                    user=self.user,
                    attendee_name="Pat Owner",
                    unloading_done=True,
                    put_into_service=True,
                )
                transition_case(self.case, "delivered", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "delivered")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class TransportFinalProtocolTests(TestCase):
    """TRP-09 "Final protocol": the final protocol IS the shared
    handover stack - the SVC-18 comparison against the TRP-06 intake
    record and the SVC-19 closing guard work for transport cases."""

    def setUp(self):
        from apps.cases.tests import build_workflow

        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(
            username="fd", password="demo12345", is_staff=True
        )
        Case = django_apps.get_model("cases", "Case")
        # in_transit belongs in the chain even though this suite starts
        # at delivered: TRP-08 reads "has the asset departed" from
        # Status.sort_order, and a workflow missing the status cannot answer.
        self.statuses = build_workflow(
            "TRANSPORT", ["in_transit", "delivered", "closed"]
        )
        self.case = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=self.statuses["delivered"],
        )
        # §7.2: an intake record is a receipt for a particular asset.
        link_identified_asset(self.case)
        self.order = TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        # TRP-09: the final protocol is drawn up after the delivery, so
        # the delivery protocol is what makes the case ready for one.
        record_delivery_confirmation(
            self.case,
            user=self.user,
            attendee_name="Pat Owner",
            unloading_done=True,
            put_into_service=True,
        )

    def _signed_intake_with_items(self):
        from apps.attachments.services import attach_file
        from apps.intake.services import (
            add_intake_item,
            create_intake_record,
            sign_intake_record,
        )

        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        record = create_intake_record(
            self.case,
            self.user,
            mount_status=IntakeRecord.MountStatus.ON_MOUNT,
            cleanliness_status=IntakeRecord.CleanlinessStatus.CLEAN,
            damage_note="Scratch on port bow.",
            energy_level=IntakeRecord.EnergyLevel.HALF,  # §7.6
        )
        add_intake_item(
            record, self.user,
            category="EQUIPMENT", item_name="Anchor", quantity=1,
        )
        add_intake_item(
            record, self.user,
            category="KEY", item_name="Ignition key", quantity=2,
        )
        Attachment = django_apps.get_model("attachments", "Attachment")
        attach_file(
            self.case,
            SimpleUploadedFile("arrival.jpg", b"fake-jpeg"),
            Attachment.Kind.PHOTO,
            Attachment.Stage.INTAKE,
            user=self.user,
        )
        sign_intake_record(record, self.user)
        return record

    def test_comparison_surfaces_intake_items_without_work_order(self):
        from apps.intake.services import (
            compare_handover_with_intake,
            create_handover_record,
        )

        record = self._signed_intake_with_items()
        handover = create_handover_record(
            self.case, self.user, discrepancies_note="Fender missing."
        )
        comparison = compare_handover_with_intake(handover)
        self.assertEqual(comparison["intake_record"], record)
        self.assertEqual(
            [item["item_name"] for item in comparison["items"]],
            ["Anchor", "Ignition key"],
        )
        self.assertEqual(comparison["discrepancies_note"], "Fender missing.")
        # Transport cases have no work order - the summary stays empty.
        self.assertEqual(comparison["work_log"], [])
        self.assertEqual(comparison["used_parts"], [])

    def test_close_path_requires_signed_handover_and_settlement(self):
        from apps.attachments.models import Attachment
        from apps.attachments.services import attach_file
        from apps.cases.exceptions import GuardCheckFailed
        from apps.cases.services import transition_case
        from apps.intake.services import (
            attach_handover_approval,
            create_handover_record,
            sign_handover,
            sign_handover_technician,
        )

        self._signed_intake_with_items()
        handover = create_handover_record(self.case, self.user)

        with patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                # Unsigned handover: closing is refused (SVC-19).
                with self.assertRaises(GuardCheckFailed):
                    transition_case(self.case, "closed", self.user)

                # The client signature itself is gated on the worker
                # signature and the approval scan (procedure order).
                sign_handover_technician(handover, self.user)
                # §7/SVC-07: the departure photos, same as at intake,
                # filed on the record this handover belongs to.
                attach_file(
                    handover,
                    SimpleUploadedFile("handover.jpg", b"fake-jpeg"),
                    Attachment.Kind.PHOTO,
                    Attachment.Stage.HANDOVER,
                    user=self.user,
                )
                attach_handover_approval(
                    handover,
                    SimpleUploadedFile("handover.pdf", b"%PDF-1.4 scan"),
                    self.user,
                )
                sign_handover(handover, "Pat Owner")

                # Still refused: not financially settled.
                with self.assertRaises(GuardCheckFailed):
                    transition_case(self.case, "closed", self.user)

                self.case.financially_settled = True
                self.case.save(update_fields=["financially_settled"])
                transition_case(self.case, "closed", self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "closed")


class PaidAdvanceDepartureGuardTests(TestCase):
    """TRP-04/INV-06 integration: mark_advance_paid unlocks
    departure_scheduled."""

    def test_paid_advance_satisfies_departure_guard(self):
        from unittest.mock import patch as mock_patch

        from apps.cases.services import transition_case
        from apps.cases.tests import build_workflow
        from apps.quotes.models import AdvancePayment
        from apps.quotes.services import mark_advance_paid

        User = django_apps.get_model("accounts", "User")
        user = User.objects.create_user(username="fd2", password="demo12345")
        Case = django_apps.get_model("cases", "Case")
        statuses = build_workflow(
            "TRANSPORT", ["advance_paid", "departure_scheduled"]
        )
        case = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=statuses["advance_paid"],
        )
        order = TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
            preferred_date=date(2027, 8, 3),
        )
        # TRP-03: a specialized carrier is doing this one, so their own
        # offer is on file before the departure is organized.
        declare_carrier_price(
            order, user=user, carrier_name="Carrier Ltd",
            price=Decimal("1000.00"),
        )
        advance = AdvancePayment.objects.create(
            case=case, percent=50, amount=Decimal("500.00")
        )

        with mock_patch("apps.notifications.services.notify_case_transition"):
            with self.captureOnCommitCallbacks(execute=True):
                # Unpaid advance: the guard refuses.
                from apps.cases.exceptions import GuardCheckFailed

                with self.assertRaises(GuardCheckFailed):
                    transition_case(case, "departure_scheduled", user)

                mark_advance_paid(advance, user)
                transition_case(case, "departure_scheduled", user)
        case.refresh_from_db()
        self.assertEqual(case.status.code, "departure_scheduled")


class RecommendedModeTest(TestCase):
    """TRP-03: the asset length recommends the transport mode."""

    def test_short_asset_recommends_own_vehicle(self):
        self.assertEqual(
            recommended_mode(Decimal("9.99")), TransportOrder.Mode.OWN_VEHICLE
        )

    def test_exactly_10m_recommends_own_vehicle(self):
        # The threshold is inclusive (~10 m may still be towed).
        self.assertEqual(
            recommended_mode(Decimal("10.00")), TransportOrder.Mode.OWN_VEHICLE
        )

    def test_long_asset_recommends_external_carrier(self):
        self.assertEqual(
            recommended_mode(Decimal("10.01")),
            TransportOrder.Mode.EXTERNAL_CARRIER,
        )

    def test_unknown_length_recommends_nothing(self):
        self.assertIsNone(recommended_mode(None))


@override_settings(STAFF_NOTIFICATION_EMAILS=["ops@example.com"])
class DeclareCarrierPriceTest(TestCase):
    """TRP-03: declare the found external-carrier price for a big asset."""

    def _order(self, length="12.00"):
        case = build_transport_case(asset_length_m=length)
        return TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )

    def _user(self):
        User = django_apps.get_model("accounts", "User")
        return User.objects.create_user(username="fd", password="demo12345")

    def test_declares_price_logs_and_notifies_staff(self):
        order = self._order()
        declare_carrier_price(
            order, Decimal("4200.00"), self._user(), carrier_name="Carrier Ltd"
        )
        order.refresh_from_db()
        self.assertEqual(order.carrier_price, Decimal("4200.00"))
        self.assertIsNotNone(order.carrier_price_declared_at)
        self.assertEqual(order.carrier_name, "Carrier Ltd")

        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=order.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("TRP-03", entry.text)
        self.assertIn("4200.00", entry.text)

        # Staff notified (email + EmailLog); no SMS gateway exists.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@example.com"])
        EmailLog = django_apps.get_model("notifications", "EmailLog")
        self.assertTrue(EmailLog.objects.filter(case=order.case).exists())

    def test_second_declaration_is_refused(self):
        order = self._order()
        user = self._user()
        declare_carrier_price(order, Decimal("4200.00"), user)
        first = order.carrier_price_declared_at
        with self.assertRaises(ValidationError):
            declare_carrier_price(order, Decimal("5000.00"), user)
        order.refresh_from_db()
        self.assertEqual(order.carrier_price, Decimal("4200.00"))
        self.assertEqual(order.carrier_price_declared_at, first)

    def test_missing_price_is_refused(self):
        order = self._order()
        with self.assertRaises(ValidationError):
            declare_carrier_price(order, None, self._user())


@override_settings(STAFF_NOTIFICATION_EMAILS=[])
class DeclareCarrierPriceNoStaffTest(TestCase):
    """No staff addresses configured: the price is still recorded and the
    notification is skipped without raising (the SYSTEM log remains)."""

    def test_records_price_without_email_or_error(self):
        case = build_transport_case(asset_length_m="12.00")
        order = TransportOrder.objects.create(
            case=case, origin="North depot", destination="South depot",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        User = django_apps.get_model("accounts", "User")
        user = User.objects.create_user(username="fd", password="demo12345")
        declare_carrier_price(order, Decimal("4200.00"), user)
        order.refresh_from_db()
        self.assertEqual(order.carrier_price, Decimal("4200.00"))
        self.assertEqual(len(mail.outbox), 0)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertTrue(
            ActivityLog.objects.filter(case=case, text__contains="TRP-03").exists()
        )


class DerivedTransportAdvanceTests(TestCase):
    """TRP-04: the mandatory 50% advance is derived from the offer, never
    typed - and a paid one is never rewritten."""

    def setUp(self):
        self.case = build_transport_case(asset_length_m="8.0")
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(username="adv1", password="demo12345")

    def _offer(self, amount="1200.00", send=True):
        from apps.quotes.services import send_quote

        quote = create_transport_offer(
            self.case, description="Transport",amount=Decimal(amount), user=self.user
        )
        if send:
            send_quote(quote, self.user)
        return quote

    def test_no_offer_derives_nothing(self):
        self.assertIsNone(ensure_transport_advance(self.case, user=self.user))
        self.assertFalse(self.case.advance_payments.exists())

    def test_draft_offer_is_not_payable_yet(self):
        self._offer(send=False)
        self.assertIsNone(ensure_transport_advance(self.case, user=self.user))

    def test_sent_offer_derives_half_of_the_amount(self):
        # TRP-01 records the desired date on the inquiry, before the offer.
        TransportOrder.objects.create(
            case=self.case, origin="North depot", destination="Zone A",
            preferred_date=date(2027, 8, 20),
        )
        self._offer(amount="1200.00")
        advance = ensure_transport_advance(self.case, user=self.user)
        self.assertEqual(advance.percent, 50)
        self.assertEqual(advance.amount, Decimal("600.00"))
        # TRP-04: due 7 days before the planned departure.
        self.assertEqual(advance.due_date, date(2027, 8, 13))

    def test_rounds_to_cents(self):
        self._offer(amount="1000.01")
        advance = ensure_transport_advance(self.case, user=self.user)
        self.assertEqual(advance.amount, Decimal("500.01"))

    def test_idempotent_and_resyncs_a_revised_offer(self):
        self._offer(amount="1200.00")
        first = ensure_transport_advance(self.case, user=self.user)
        again = ensure_transport_advance(self.case, user=self.user)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(self.case.advance_payments.count(), 1)

        revision = self._offer(amount="1500.00")
        resynced = ensure_transport_advance(self.case, user=self.user)
        self.assertEqual(resynced.pk, first.pk)
        self.assertEqual(resynced.amount, Decimal("750.00"))
        self.assertEqual(resynced.quote_id, revision.pk)

    def test_a_paid_advance_is_never_rewritten(self):
        from apps.quotes.services import mark_advance_paid

        self._offer(amount="1200.00")
        advance = ensure_transport_advance(self.case, user=self.user)
        mark_advance_paid(advance, self.user)

        self._offer(amount="1500.00")
        kept = ensure_transport_advance(self.case, user=self.user)
        self.assertEqual(kept.pk, advance.pk)
        self.assertEqual(kept.amount, Decimal("600.00"))
        self.assertEqual(self.case.advance_payments.count(), 1)

    def test_non_transport_case_is_left_alone(self):
        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type="SERVICE", code="new_report", name="n", client_visible_name="n"
        )
        case = Case.objects.create(
            case_type="SERVICE", report_channel="PHONE", status=status
        )
        self.assertIsNone(ensure_transport_advance(case, user=self.user))
