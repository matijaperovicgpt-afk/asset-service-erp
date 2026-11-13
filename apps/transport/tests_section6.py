"""Procedure §6 "Asset transport procedure".

One class per sub-step, in the order the procedure states them. Each
pins a rule that was previously unenforced or enforced only by a form -
what the audit found is written into the docstrings, so a failing test
here says which sentence of the procedure stopped being true.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.cases.exceptions import GuardCheckFailed
from apps.cases.services import transition_case
from apps.cases.tests import build_workflow

from .models import (
    DeliveryConfirmation,
    LoadingConfirmation,
    LoadingPlan,
    StandardDepartureDay,
    TransportOrder,
)
from .services import (
    declare_carrier_price,
    ensure_transport_advance,
    record_delivery_confirmation,
    record_loading_confirmation,
    record_loading_plan,
)

TRANSPORT_CODES = [
    "inquiry",
    "quote_sent",
    "advance_paid",
    "departure_scheduled",
    "in_transit",
    "delivered",
    "closed",
]


class TransportCaseMixin:
    """A transport case with the full seeded chain, and the smallest
    helpers that make each later stage legitimately reachable."""

    def setUp(self):
        super().setUp()
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(
            username="s6", password="demo12345", is_staff=True
        )
        Case = django_apps.get_model("cases", "Case")
        self.statuses = build_workflow("TRANSPORT", TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type="TRANSPORT",
            report_channel="PHONE",
            status=self.statuses["inquiry"],
        )

    # -- building blocks ---------------------------------------------------
    def order(self, **overrides):
        fields = dict(
            case=self.case,
            origin="Customer site",
            destination="Zone A",
            asset_length_m=Decimal("8.50"),
            mode=TransportOrder.Mode.OWN_VEHICLE,
            # QA T-M2: departure_scheduled needs an agreed date.
            preferred_date=date.today() + timedelta(days=30),
        )
        fields.update(overrides)
        return TransportOrder.objects.create(**fields)

    def sent_quote(self, amount="1200.00"):
        Quote = django_apps.get_model("quotes", "Quote")
        return Quote.objects.create(
            case=self.case,
            description="Transport Northtown - Zone A",
            amount=Decimal(amount),
            status=Quote.Status.SENT,
        )

    def paid_advance(self, amount="600.00"):
        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        return AdvancePayment.objects.create(
            case=self.case,
            percent=50,
            amount=Decimal(amount),
            paid_at=timezone.now(),
        )

    def signed_intake(self):
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        return IntakeRecord.objects.create(
            case=self.case, received_by=self.user, signed_at=timezone.now()
        )

    def agreed_plan(self, order, **overrides):
        fields = dict(
            unloading_location="Depot D, ramp 2",
            lifting_availability=LoadingPlan.CraneAvailability.AVAILABLE,
            attendee="Pat Owner",
            loading_at=timezone.now() + timedelta(days=3),
            unloading_at=timezone.now() + timedelta(days=4),
        )
        fields.update(overrides)
        return record_loading_plan(self.case, user=self.user, **fields)

    def at(self, code):
        self.case.status = self.statuses[code]
        self.case.save(update_fields=["status"])
        return self.case

    def move(self, code):
        return transition_case(self.case, code, self.user)


class Substep1InquiryAndOfferTests(TransportCaseMixin, TestCase):
    """§6.1 "Inquiry and offer".

    Both halves used to live only in the guide read-model, which greys out
    one button while the Transitions accordion renders a live one beside
    it - and ``auto_advance_case`` ignores the guide entirely.
    """

    def test_quote_sent_refused_without_any_inquiry(self):
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("quote_sent")
        self.assertIn("TRP-01", str(ctx.exception))

    def test_quote_sent_refused_on_an_undocumented_inquiry(self):
        # A bare row satisfied the old "is not None" predicate.
        TransportOrder.objects.create(case=self.case)
        self.sent_quote()
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("quote_sent")
        message = str(ctx.exception)
        self.assertIn("the origin site", message)
        self.assertIn("the delivery place", message)

    def test_quote_sent_refused_while_the_offer_is_still_a_draft(self):
        self.order()
        Quote = django_apps.get_model("quotes", "Quote")
        Quote.objects.create(
            case=self.case,
            description="Transport",
            amount=Decimal("1200.00"),
            status=Quote.Status.DRAFT,
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("quote_sent")
        self.assertIn("no offer has been sent", str(ctx.exception))

    def test_quote_sent_allowed_once_the_offer_is_out(self):
        self.order()
        self.sent_quote()
        self.move("quote_sent")
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "quote_sent")

    def test_a_asset_on_the_case_counts_as_the_asset_datum(self):
        # The length may live on the linked asset rather than the order.
        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")
        client = Client.objects.create(name="John Smith")
        self.case.asset = Asset.objects.create(
            client=client, name="Unit 7", asset_type="Generator",
            length_m=Decimal("7.00"),
        )
        self.case.save(update_fields=["asset"])
        self.order(asset_length_m=None)
        self.sent_quote()
        self.move("quote_sent")
        self.assertEqual(self.case.status.code, "quote_sent")


class Substep1AdvanceStageTests(TransportCaseMixin, TestCase):
    """§6.1/§6.4: ``advance_paid`` had no guard either, so a case could
    claim the stage with nothing to pay."""

    def test_advance_paid_refused_with_no_advance_in_existence(self):
        self.order()
        self.sent_quote()
        self.at("quote_sent")
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("advance_paid")
        self.assertIn("TRP-04", str(ctx.exception))

    def test_advance_paid_refused_while_the_derived_advance_is_unpaid(self):
        """QA T-M1: the status read "Advance paid" as soon as the offer
        went out, with the advance still unpaid."""
        self.order()
        self.sent_quote()
        ensure_transport_advance(self.case, user=self.user)
        self.at("quote_sent")
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("advance_paid")
        self.assertIn("marked paid", str(ctx.exception))

    def test_advance_paid_allowed_once_the_advance_is_paid(self):
        self.order()
        self.sent_quote()
        advance = ensure_transport_advance(self.case, user=self.user)
        advance.paid_at = timezone.now()
        advance.save(update_fields=["paid_at"])
        self.at("quote_sent")
        self.move("advance_paid")
        self.assertEqual(self.case.status.code, "advance_paid")


class Substep2LoadingPlanTests(TransportCaseMixin, TestCase):
    """§6.2 "Loading and unloading plan".

    The model, the service and the form all existed; no URL, view,
    template or admin page reached any of them, and no guard asked for
    the plan - so a truck could be dispatched to a site with no crane
    and nobody expecting it.
    """

    def setUp(self):
        super().setUp()
        self.transport_order = self.order()
        self.sent_quote()
        self.paid_advance()
        self.signed_intake()
        self.at("departure_scheduled")

    def test_departure_refused_with_no_plan_at_all(self):
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("in_transit")
        message = str(ctx.exception)
        self.assertIn("TRP-02", message)
        self.assertIn("the unloading location", message)

    def test_departure_refused_while_the_crane_is_still_to_be_arranged(self):
        self.agreed_plan(
            self.transport_order,
            lifting_availability=LoadingPlan.CraneAvailability.TO_ARRANGE,
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("in_transit")
        self.assertIn("lifting equipment availability", str(ctx.exception))

    def test_departure_refused_with_no_unloading_time(self):
        self.agreed_plan(self.transport_order, unloading_at=None)
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("in_transit")
        self.assertIn("the unloading time", str(ctx.exception))

    def ready_loading(self):
        return record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRUCK,
            readiness=LoadingConfirmation.Readiness.READY,
        )

    def test_departure_allowed_once_the_plan_is_agreed(self):
        self.agreed_plan(self.transport_order)
        self.ready_loading()
        self.move("in_transit")
        self.assertEqual(self.case.status.code, "in_transit")

    def test_departure_refused_on_the_plan_alone(self):
        """QA T-H1: saving the plan moved the case "U transportu" with
        nothing loaded."""
        self.agreed_plan(self.transport_order)
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("in_transit")
        self.assertIn("TRP-07", str(ctx.exception))

    def test_departure_refused_before_the_booked_day(self):
        Appointment = django_apps.get_model("scheduling", "Appointment")
        Appointment.objects.create(
            case=self.case,
            kind=Appointment.Kind.TRANSPORT_DEPARTURE,
            scheduled_for=timezone.now() + timedelta(days=14),
        )
        self.agreed_plan(self.transport_order)
        self.ready_loading()
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("in_transit")
        self.assertIn("still ahead", str(ctx.exception))

    def test_auto_advance_never_enters_in_transit(self):
        from apps.cases.services import auto_advance_case

        self.agreed_plan(self.transport_order)
        self.ready_loading()
        self.assertEqual(auto_advance_case(self.case, self.user), [])
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "departure_scheduled")

    def test_the_plan_is_recorded_and_logged(self):
        plan = self.agreed_plan(self.transport_order)
        self.assertEqual(plan.unloading_location, "Depot D, ramp 2")
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="TRP-02"
            ).exists()
        )


class Substep3CarrierQuoteTests(TransportCaseMixin, TestCase):
    """§6.3 "Choosing the transport method".

    The length rule stays a warning - "when dimensions and regulations
    allow" is a judgement. That an engaged carrier's own offer is
    obtained is not, and nothing used to ask for it.
    """

    def setUp(self):
        super().setUp()
        self.sent_quote()
        self.paid_advance()
        self.at("advance_paid")

    def test_departure_refused_for_a_long_asset_with_no_carrier_price(self):
        self.order(
            asset_length_m=Decimal("14.00"),
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("departure_scheduled")
        self.assertIn("TRP-03", str(ctx.exception))

    def test_the_length_alone_triggers_it_even_on_the_default_mode(self):
        # ``validate_mode`` reads the length as authoritative; so does this.
        self.order(asset_length_m=Decimal("12.40"))
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("departure_scheduled")
        self.assertIn("specialized carrier", str(ctx.exception))

    def test_departure_allowed_once_the_carrier_offer_is_declared(self):
        order = self.order(
            asset_length_m=Decimal("14.00"),
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        declare_carrier_price(
            order, user=self.user, carrier_name="Carrier Ltd",
            price=Decimal("2400.00"),
        )
        self.move("departure_scheduled")
        self.assertEqual(self.case.status.code, "departure_scheduled")

    def test_own_transport_needs_no_carrier_offer(self):
        self.order(asset_length_m=Decimal("8.50"))
        self.move("departure_scheduled")
        self.assertEqual(self.case.status.code, "departure_scheduled")


class Substep4AdvanceTests(TransportCaseMixin, TestCase):
    """§6.4 "Advance": 50%, at least seven days before departure."""

    def test_departure_refused_when_the_paid_advance_is_short(self):
        self.order()
        self.sent_quote("1200.00")
        self.paid_advance("120.00")  # a 10% row, marked paid
        self.at("advance_paid")
        with self.assertRaises(GuardCheckFailed) as ctx:
            self.move("departure_scheduled")
        message = str(ctx.exception)
        self.assertIn("TRP-04", message)
        self.assertIn("600.00", message)

    def test_departure_allowed_on_the_full_half(self):
        self.order()
        self.sent_quote("1200.00")
        self.paid_advance("600.00")
        self.at("advance_paid")
        self.move("departure_scheduled")
        self.assertEqual(self.case.status.code, "departure_scheduled")

    def test_deriving_with_no_departure_date_says_so(self):
        self.order(preferred_date=None)
        self.sent_quote()
        advance = ensure_transport_advance(self.case, user=self.user)
        self.assertIsNone(advance.due_date)
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case,
                text__contains="TRP-04 warning: the advance has no due date",
            ).exists()
        )

    def test_booking_the_departure_backfills_the_due_date(self):
        from apps.scheduling.models import Appointment
        from apps.scheduling.services import book_appointment
        from apps.scheduling.workinghours import next_working_slot

        self.order(preferred_date=None)
        self.sent_quote()
        advance = ensure_transport_advance(self.case, user=self.user)
        self.assertIsNone(advance.due_date)

        departure = next_working_slot(timezone.now() + timedelta(days=40))
        book_appointment(
            self.case,
            kind=Appointment.Kind.TRANSPORT_DEPARTURE,
            scheduled_for=departure,
            user=self.user,
        )
        advance.refresh_from_db()
        self.assertEqual(
            advance.due_date, departure.date() - timedelta(days=7)
        )


class Substep5StandardDepartureDayTests(TransportCaseMixin, TestCase):
    """§6.5 "Standardni dani polaska".

    The configuration and the soft warning were already right - what was
    missing was that the warning watched the desired term while the
    workflow books its departure elsewhere, and that the client's choice
    of a predefined day was writable only from the Django admin.
    """

    def setUp(self):
        super().setUp()
        for code, name, weekday in (
            ("TUE", "Utorak", 1),
            ("THU", "Cetvrtak", 3),
            ("SAT", "Subota", 5),
        ):
            StandardDepartureDay.objects.create(
                code=code, name=name, weekday=weekday, is_active=True
            )

    def _book(self, when):
        from apps.scheduling.models import Appointment
        from apps.scheduling.services import book_appointment

        return book_appointment(
            self.case,
            kind=Appointment.Kind.TRANSPORT_DEPARTURE,
            scheduled_for=when,
            user=self.user,
        )

    def _next_weekday(self, weekday):
        # Built at 10:00 rather than via next_working_slot, which
        # would happily shift the booking onto a different weekday - the
        # one thing this suite is measuring.
        from datetime import datetime, time as time_of_day

        day = (timezone.now() + timedelta(days=30)).date()
        while day.weekday() != weekday:
            day += timedelta(days=1)
        return timezone.make_aware(
            datetime.combine(day, time_of_day(10, 0))
        )

    def test_booking_a_non_standard_departure_is_flagged(self):
        self.order()
        self._book(self._next_weekday(0))  # a Monday
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        warnings = ActivityLog.objects.filter(
            case=self.case, text__contains="TRP-05 warning: the booked departure"
        )
        self.assertEqual(warnings.count(), 1)
        self.assertIn("Utorak", warnings.first().text)

    def test_booking_a_standard_departure_is_silent(self):
        self.order()
        self._book(self._next_weekday(1))  # a Tuesday
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertFalse(
            ActivityLog.objects.filter(
                case=self.case, text__contains="TRP-05 warning"
            ).exists()
        )

    def test_the_intake_form_offers_the_configured_days(self):
        from apps.backoffice.transport import TransportIntakeForm

        form = TransportIntakeForm(case=self.case)
        codes = [c for c, _label in form.fields["standard_departure_day"].choices]
        self.assertEqual(codes, ["", "TUE", "THU", "SAT"])


class Substep6IntakeBeforeLoadingTests(TransportCaseMixin, TestCase):
    """§6.6 "Intake record PRE transporta".

    The ``in_transit`` guard is an existence check, which an intake record
    written *after* the asset was craned satisfies just as well - so the
    document meant to prove damage predated loading could certify the
    opposite.
    """

    def setUp(self):
        super().setUp()
        self.transport_order = self.order(
            mode=TransportOrder.Mode.EXTERNAL_CARRIER
        )

    def test_loading_refused_before_any_intake(self):
        with self.assertRaises(ValidationError) as ctx:
            record_loading_confirmation(
                self.case,
                user=self.user,
                vehicle_type=LoadingConfirmation.VehicleType.TRAILER,
                readiness=LoadingConfirmation.Readiness.READY,
                carrier="Carrier Ltd",
            )
        self.assertIn("TRP-06", str(ctx.exception))
        self.assertEqual(LoadingConfirmation.objects.count(), 0)

    def test_loading_refused_while_the_intake_is_unsigned(self):
        IntakeRecord = django_apps.get_model("intake", "IntakeRecord")
        IntakeRecord.objects.create(case=self.case, received_by=self.user)
        with self.assertRaises(ValidationError):
            record_loading_confirmation(
                self.case,
                user=self.user,
                vehicle_type=LoadingConfirmation.VehicleType.TRAILER,
                readiness=LoadingConfirmation.Readiness.SECURED,
            )

    def test_loading_allowed_once_the_intake_is_signed(self):
        self.signed_intake()
        confirmation = record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRAILER,
            readiness=LoadingConfirmation.Readiness.READY,
            carrier="Carrier Ltd",
        )
        self.assertEqual(confirmation.signed_by, self.user)


class Substep7BothPartiesTests(TransportCaseMixin, TestCase):
    """§6.7: "The carrier AND the responsible person check" - two parties.

    A carrier is not a user of this app and cannot sign in it, so what is
    asked for is that they are named, and only where there is a third
    party at all.
    """

    def setUp(self):
        super().setUp()
        self.signed_intake()

    def test_ready_signoff_refused_without_naming_the_carrier(self):
        self.order(mode=TransportOrder.Mode.EXTERNAL_CARRIER)
        with self.assertRaises(ValidationError) as ctx:
            record_loading_confirmation(
                self.case,
                user=self.user,
                vehicle_type=LoadingConfirmation.VehicleType.TRUCK,
                readiness=LoadingConfirmation.Readiness.READY,
            )
        self.assertIn("TRP-07", str(ctx.exception))

    def test_nothing_verified_yet_is_still_recordable(self):
        # NOT_READY means "nothing checked", not "checked and failed" -
        # holding it to a two-party claim would be asking who performed a
        # check that has not happened.
        self.order(mode=TransportOrder.Mode.EXTERNAL_CARRIER)
        confirmation = record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRUCK,
            readiness=LoadingConfirmation.Readiness.NOT_READY,
        )
        self.assertEqual(
            confirmation.readiness, LoadingConfirmation.Readiness.NOT_READY
        )

    def test_own_transport_needs_no_carrier_named(self):
        # On an own-vehicle job the site IS the carrier; a blank is the
        # truthful answer, not a missing one.
        self.order(mode=TransportOrder.Mode.OWN_VEHICLE)
        confirmation = record_loading_confirmation(
            self.case,
            user=self.user,
            vehicle_type=LoadingConfirmation.VehicleType.TRAILER,
            readiness=LoadingConfirmation.Readiness.READY,
        )
        self.assertEqual(confirmation.carrier, "")


class Substep8DeliveryTests(TransportCaseMixin, TestCase):
    """§6.8 "Delivery": at the delivery place, unloading and launch."""

    def setUp(self):
        super().setUp()
        self.transport_order = self.order()

    def _confirm(self, **overrides):
        fields = dict(
            attendee_name="Pat Owner",
            attendee_role=DeliveryConfirmation.AttendeeRole.OWNER,
            unloading_done=True,
            put_into_service=True,
        )
        fields.update(overrides)
        return record_delivery_confirmation(
            self.case, user=self.user, **fields
        )

    def test_delivery_refused_before_the_asset_departs(self):
        self.at("quote_sent")
        with self.assertRaises(ValidationError) as ctx:
            self._confirm()
        self.assertIn("TRP-08", str(ctx.exception))
        self.assertEqual(DeliveryConfirmation.objects.count(), 0)
        self.transport_order.refresh_from_db()
        self.assertIsNone(self.transport_order.delivered_at)

    def test_delivery_refused_when_nothing_was_unloaded(self):
        self.at("in_transit")
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(unloading_done=False)
        self.assertIn("unloading that happened", str(ctx.exception))

    def test_not_putting_into_service_needs_a_stated_reason(self):
        self.at("in_transit")
        with self.assertRaises(ValidationError) as ctx:
            self._confirm(put_into_service=False)
        self.assertIn("not put into service", str(ctx.exception))

    def test_not_putting_into_service_is_legitimate_with_one(self):
        self.at("in_transit")
        confirmation = self._confirm(
            put_into_service=False, notes="Delivered onto the owner's vehicle."
        )
        self.assertFalse(confirmation.put_into_service)
        self.assertTrue(confirmation.unloading_done)

    def test_a_real_delivery_stamps_and_unlocks_delivered(self):
        self.at("in_transit")
        self._confirm()
        self.transport_order.refresh_from_db()
        self.assertIsNotNone(self.transport_order.delivered_at)
        self.move("delivered")
        self.assertEqual(self.case.status.code, "delivered")


class Substep9FinalProtocolTests(TransportCaseMixin, TestCase):
    """§6.9 "AFTER delivery ... the handover record is signed"."""

    def setUp(self):
        super().setUp()
        self.transport_order = self.order()

    def test_handover_refused_before_the_delivery_protocol(self):
        from apps.intake.services import create_handover_record

        self.at("in_transit")
        with self.assertRaises(ValidationError) as ctx:
            create_handover_record(self.case, self.user)
        self.assertIn("TRP-09", str(ctx.exception))

    def test_handover_allowed_once_the_delivery_is_recorded(self):
        from apps.intake.services import create_handover_record

        self.at("in_transit")
        record_delivery_confirmation(
            self.case,
            user=self.user,
            attendee_name="Pat Owner",
            unloading_done=True,
            put_into_service=True,
        )
        handover = create_handover_record(self.case, self.user)
        self.assertEqual(handover.case, self.case)

    def test_work_location_is_a_service_question(self):
        # Setting an on-site work location on a transport case switched
        # off ``requires_asset_intake``, which took the whole final
        # protocol requirement with it.
        from apps.cases.models import WorkLocation
        from apps.cases.services import record_work_location_consult

        with self.assertRaises(ValidationError) as ctx:
            record_work_location_consult(
                self.case, WorkLocation.ON_SITE, self.user
            )
        self.assertIn("SVC-03", str(ctx.exception))
        self.case.refresh_from_db()
        self.assertTrue(self.case.requires_asset_intake)


class AutoAdvanceOvershootTests(TransportCaseMixin, TestCase):
    """The reason §6.1's guards matter beyond the wizard.

    ``auto_advance_case`` walks every linear single-edge hop whose guards
    pass, after every successful card action. With no guard on
    ``quote_sent`` or ``advance_paid``, drafting the offer carried the
    case two statuses forward - past the very act it was recording.
    """

    def test_a_draft_offer_no_longer_carries_the_case_forward(self):
        from apps.cases.services import auto_advance_case

        self.order()
        Quote = django_apps.get_model("quotes", "Quote")
        Quote.objects.create(
            case=self.case,
            description="Transport",
            amount=Decimal("1200.00"),
            status=Quote.Status.DRAFT,
        )
        auto_advance_case(self.case, self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "inquiry")

    def test_a_sent_offer_advances_exactly_one_hop(self):
        from apps.cases.services import auto_advance_case

        self.order()
        self.sent_quote()
        # No advance exists yet, so ``advance_paid`` refuses and the walk
        # stops where the work actually is.
        auto_advance_case(self.case, self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "quote_sent")


class LoadingPlanScreenTests(TransportCaseMixin, TestCase):
    """§6.2 had no way in at all: no URL, no view, no template, and not
    even a Django admin registration."""

    def setUp(self):
        super().setUp()
        self.transport_order = self.order()
        self.client.force_login(self.user)

    def test_the_plan_posts_from_the_case_page(self):
        from django.urls import reverse

        when = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
        response = self.client.post(
            reverse("backoffice:transport-plan", args=[self.case.pk]),
            {
                "unloading_location": "Depot D, ramp 2",
                "lifting_availability": LoadingPlan.CraneAvailability.AVAILABLE,
                "lifting_notes": "Site crane, 12 t.",
                "site_conditions": "Narrow approach, book the ramp.",
                "attendee": "Pat Owner",
                "loading_at_0": when,
                "loading_at_1": "08:00",
                "unloading_at_0": when,
                "unloading_at_1": "16:00",
                "notes": "",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        plan = LoadingPlan.objects.get(transport_order=self.transport_order)
        self.assertEqual(plan.attendee, "Pat Owner")
        self.assertIsNotNone(plan.loading_at)
        self.assertIsNotNone(plan.unloading_at)

    def test_the_card_renders_on_the_case_page(self):
        from django.urls import reverse

        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "transport/plan/")
        self.assertContains(response, 'name="unloading_location"')
