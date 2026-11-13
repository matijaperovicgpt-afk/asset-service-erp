"""Tests for TRP-01 "Inquiry and offer": remembered inquiry fields, the
model x zone price matrix, the offer flow and saved pickup times.

Kept in a separate module from ``tests.py`` so the inquiry/pricing work
stays additive alongside the other transport rules.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from .models import (
    DeparturePickupSlot,
    DestinationZone,
    TransportOrder,
    TransportPriceMatrix,
)
from .services import (
    available_pickup_slots,
    create_transport_offer,
    price_for,
    record_transport_inquiry,
    suggest_transport_offer,
    upsert_transport_order,
    validate_mode,
    validate_pickup_slot,
)


def build_transport_case(asset_length_m=None):
    """Create a TRANSPORT case, optionally with a client + asset."""
    Status = django_apps.get_model("cases", "Status")
    Case = django_apps.get_model("cases", "Case")
    status, _ = Status.objects.get_or_create(
        case_type="TRANSPORT",
        code="inquiry",
        defaults={"name": "Inquiry", "client_visible_name": "Received"},
    )
    asset = None
    client = None
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
        client=client,
        asset=asset,
    )


def _order(case, **kwargs):
    return TransportOrder.objects.create(
        case=case, origin="EU luka", destination="Zone A",
        mode=TransportOrder.Mode.EXTERNAL_CARRIER, **kwargs,
    )


class PriceMatrixTests(TestCase):
    """TRP-01: model x zone lookup, incl. NULL price = negotiable."""

    def setUp(self):
        TransportPriceMatrix.objects.create(
            model_code="M20", destination_zone=DestinationZone.ZONE_A,
            price=Decimal("3400"),
        )
        TransportPriceMatrix.objects.create(
            model_code="M20", destination_zone=DestinationZone.ZONE_B,
            price=Decimal("4600"),
        )
        TransportPriceMatrix.objects.create(
            model_code="60 ST", destination_zone=DestinationZone.ZONE_A,
            price=None,
        )

    def test_price_hit_per_zone(self):
        case = build_transport_case()
        order = _order(
            case, asset_model="M20", destination_zone=DestinationZone.ZONE_A
        )
        self.assertEqual(price_for(order), Decimal("3400"))
        order.destination_zone = DestinationZone.ZONE_B
        self.assertEqual(price_for(order), Decimal("4600"))

    def test_case_insensitive_model_match(self):
        case = build_transport_case()
        order = _order(
            case, asset_model=" m20 ", destination_zone=DestinationZone.ZONE_A
        )
        self.assertEqual(price_for(order), Decimal("3400"))

    def test_negotiable_price_is_none(self):
        case = build_transport_case()
        order = _order(
            case, asset_model="60 ST", destination_zone=DestinationZone.ZONE_A
        )
        self.assertIsNone(price_for(order))

    def test_missing_model_or_zone_is_none(self):
        case = build_transport_case()
        self.assertIsNone(price_for(_order(case, asset_model="M20")))
        self.assertIsNone(
            price_for(_order(build_transport_case(),
                             destination_zone=DestinationZone.ZONE_A))
        )

    def test_inactive_row_gives_no_suggestion(self):
        TransportPriceMatrix.objects.filter(model_code="M20").update(
            is_active=False
        )
        case = build_transport_case()
        order = _order(
            case, asset_model="M20", destination_zone=DestinationZone.ZONE_A
        )
        self.assertIsNone(price_for(order))


class UpsertAndRecordTests(TestCase):
    """TRP-01: incremental capture + audit log."""

    def _activity_log(self):
        return django_apps.get_model("cases", "ActivityLog")

    def test_upsert_is_incremental(self):
        case = build_transport_case()
        upsert_transport_order(case, asset_model="M20")
        upsert_transport_order(case, origin="Split", destination="South depot")
        order = TransportOrder.objects.get(case=case)
        self.assertEqual(order.asset_model, "M20")
        self.assertEqual(order.origin, "Split")
        self.assertEqual(TransportOrder.objects.filter(case=case).count(), 1)

    def test_record_inquiry_logs_with_price_note(self):
        TransportPriceMatrix.objects.create(
            model_code="M20", destination_zone=DestinationZone.ZONE_A,
            price=Decimal("3400"),
        )
        case = build_transport_case()
        record_transport_inquiry(
            case,
            asset_brand="Acme",
            asset_model="M20",
            origin="EU luka",
            destination="Zone A",
            destination_zone=DestinationZone.ZONE_A,
            preferred_date=date(2026, 9, 1),
        )
        entry = self._activity_log().objects.get(case=case)
        self.assertEqual(entry.kind, entry.Kind.SYSTEM)
        self.assertIn("TRP-01", entry.text)
        self.assertIn("3400", entry.text)

    def test_record_inquiry_rejects_non_transport(self):
        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type="SERVICE", code="new_report", name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type="SERVICE", report_channel="PHONE", status=status
        )
        with self.assertRaises(ValidationError):
            record_transport_inquiry(case, asset_model="M20")


class ValidateModeFallbackTests(TestCase):
    """TRP-03: the order's own length is authoritative when no asset
    is linked."""

    def test_order_length_over_10m_warns_without_linked_asset(self):
        case = build_transport_case()
        order = TransportOrder.objects.create(
            case=case, origin="EU luka", destination="Zone A",
            mode=TransportOrder.Mode.OWN_VEHICLE,
            asset_model="A46 LT", asset_length_m=Decimal("14.00"),
        )
        warning = validate_mode(order)
        self.assertIsNotNone(warning)
        self.assertIn("external carrier", warning)


class OfferTests(TestCase):
    """TRP-01 "Offer": auto-priced draft quote with scope + route."""

    def test_suggest_prefills_scope_and_price(self):
        TransportPriceMatrix.objects.create(
            model_code="M20", destination_zone=DestinationZone.ZONE_A,
            price=Decimal("3400"),
        )
        case = build_transport_case()
        upsert_transport_order(
            case, asset_model="M20", destination_zone=DestinationZone.ZONE_A
        )
        suggestion = suggest_transport_offer(case)
        self.assertEqual(suggestion["amount"], Decimal("3400"))
        self.assertIn("Loading", suggestion["description"])

    def test_create_offer_makes_draft_quote_with_route(self):
        Quote = django_apps.get_model("quotes", "Quote")
        case = build_transport_case()
        upsert_transport_order(
            case, origin="EU luka", destination="Zone A",
            destination_zone=DestinationZone.ZONE_A,
        )
        quote = create_transport_offer(
            case,
            description="Loading; transport; unloading.",
            amount=Decimal("3400"),
        )
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertIn("EU luka", quote.description)
        self.assertIn("Zone A", quote.description)
        case.transport_order.refresh_from_db()
        self.assertEqual(case.transport_order.estimated_cost, Decimal("3400"))

    def test_create_offer_rejects_non_transport(self):
        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type="SERVICE", code="new_report", name="n",
            client_visible_name="n",
        )
        case = Case.objects.create(
            case_type="SERVICE", report_channel="PHONE", status=status
        )
        with self.assertRaises(ValidationError):
            create_transport_offer(case, description="x", amount=Decimal("1"))


class PickupSlotTests(TestCase):
    """TRP-01: saved available pickup times + soft date validation."""

    def setUp(self):
        self.today = timezone.localdate()
        self.future = DeparturePickupSlot.objects.create(
            date=self.today + timedelta(days=7), origin="EU luka",
        )
        DeparturePickupSlot.objects.create(
            date=self.today + timedelta(days=1),
            status=DeparturePickupSlot.Status.BOOKED,
        )
        DeparturePickupSlot.objects.create(
            date=self.today - timedelta(days=1),
        )

    def test_available_slots_are_future_and_available(self):
        slots = list(available_pickup_slots())
        self.assertEqual(slots, [self.future])

    def test_validate_pickup_slot_silent_on_match(self):
        case = build_transport_case()
        order = _order(case, preferred_date=self.future.date)
        self.assertIsNone(validate_pickup_slot(order))

    def test_validate_pickup_slot_warns_off_slot(self):
        case = build_transport_case()
        order = _order(case, preferred_date=self.today + timedelta(days=3))
        warning = validate_pickup_slot(order)
        self.assertIsNotNone(warning)
        self.assertIn("TRP-01", warning)
        entry = django_apps.get_model("cases", "ActivityLog").objects.get(
            case=case
        )
        self.assertEqual(entry.text, warning)
