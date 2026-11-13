"""Tests for the TRP-01 transport inquiry, price-list and offer pages."""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel
from apps.cases.tests import build_workflow
from apps.clients.models import Client, Asset
from apps.portal.forms import OTHER_LOCATION
from apps.transport.models import (
    DeparturePickupSlot,
    DestinationZone,
    StandardDepartureDay,
    TransportOrder,
    TransportPriceMatrix,
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


class TransportIntakeViewTests(TestCase):
    def setUp(self):
        self.statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="John Smith",
        )
        self.staff = get_user_model().objects.create_user(
            username="tr_staff", password="demo12345", is_staff=True
        )
        self.client.force_login(self.staff)
        self.url = reverse("backoffice:transport-intake", args=[self.case.pk])

    def _post_data(self, **overrides):
        data = {
            "existing_client": "",
            "name": "John Smith",
            "phone": "+44 67 111 222",
            "email": "",
            "asset_name": "Unit 7",
            "asset_type": "Generator",
            "asset_registration": "TV-1",
            "asset_brand": "Acme",
            "asset_model": "M20",
            "asset_length_m": "6.50",
            "origin_site": OTHER_LOCATION,
            "origin_site_other": "EU luka",
            "destination": "Zone A",
            "destination_zone": DestinationZone.ZONE_A,
            "preferred_date": "",
            "pickup_slot": "",
            "mode": TransportOrder.Mode.OWN_VEHICLE,
        }
        data.update(overrides)
        return data

    def test_get_renders(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_non_staff_gets_403(self):
        outsider = get_user_model().objects.create_user(
            username="out", password="demo12345", is_staff=False
        )
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_non_transport_case_404s(self):
        service_status = build_workflow(CaseType.SERVICE, ["new_report"])
        service_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=service_status["new_report"],
            report_channel=ReportChannel.PHONE,
        )
        url = reverse("backoffice:transport-intake", args=[service_case.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_post_records_inquiry_and_links(self):
        TransportPriceMatrix.objects.create(
            model_code="M20", destination_zone=DestinationZone.ZONE_A,
            price=Decimal("3400"),
        )
        response = self.client.post(self.url, self._post_data())
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertIsNotNone(self.case.client_id)
        self.assertIsNotNone(self.case.asset_id)
        order = self.case.transport_order
        self.assertEqual(order.asset_model, "M20")
        self.assertEqual(order.origin, "EU luka")
        self.assertEqual(order.destination_zone, DestinationZone.ZONE_A)
        self.assertTrue(
            ActivityLog.objects.filter(case=self.case, text__contains="TRP-01").exists()
        )


    def test_reopening_an_inquiry_keeps_the_origin_site(self):
        """The intake form is the only way to edit an inquiry.

        ``origin_site`` is required but was missing from the prefill, so
        reopening a saved inquiry and pressing Save was rejected outright -
        the screen could be opened but never used a second time.
        """
        self.client.post(
            self.url,
            self._post_data(
                origin_site="Main workshop",
                origin_site_other="",
            ),
        )
        form = self.client.get(self.url).context["form"]
        self.assertEqual(
            form.initial["origin_site"], "Main workshop"
        )

        response = self.client.post(
            self.url,
            self._post_data(
                origin_site="Main workshop",
                origin_site_other="",
                destination="Eastville",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        order = self.case.transport_order
        self.assertEqual(order.origin, "Main workshop")
        self.assertEqual(order.destination, "Eastville")

    def test_reopening_an_inquiry_keeps_a_free_text_origin(self):
        """The destructive variant: the typed site name was unrecoverable
        from the page, since both the select and the Other box came back
        empty."""
        self.client.post(self.url, self._post_data())
        form = self.client.get(self.url).context["form"]
        self.assertEqual(form.initial["origin_site"], OTHER_LOCATION)
        self.assertEqual(form.initial["origin_site_other"], "EU luka")

    def test_resaving_an_inquiry_keeps_the_booked_pickup_slot(self):
        """The slot was written once and silently NULLed on the next save."""
        import datetime

        slot = DeparturePickupSlot.objects.create(
            date=datetime.date(2027, 6, 10), time=datetime.time(8, 0)
        )
        self.client.post(self.url, self._post_data(pickup_slot=str(slot.pk)))
        self.case.refresh_from_db()
        self.assertEqual(self.case.transport_order.pickup_slot_id, slot.pk)

        form = self.client.get(self.url).context["form"]
        self.assertEqual(form.initial["pickup_slot"], slot.pk)

        self.client.post(
            self.url, self._post_data(pickup_slot=str(slot.pk))
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.transport_order.pickup_slot_id, slot.pk)

    def test_a_booked_slot_stays_selectable_after_it_leaves_the_menu(self):
        """A slot that is no longer available must still be re-selectable,
        or the term the client agreed to cannot be resubmitted at all."""
        import datetime

        slot = DeparturePickupSlot.objects.create(
            date=datetime.date(2027, 6, 10), time=datetime.time(8, 0)
        )
        self.client.post(self.url, self._post_data(pickup_slot=str(slot.pk)))
        slot.status = DeparturePickupSlot.Status.BOOKED
        slot.save(update_fields=["status"])

        form = self.client.get(self.url).context["form"]
        self.assertIn(slot, form.fields["pickup_slot"].queryset)

    def test_an_untouched_mode_is_derived_not_invented(self):
        """A blank Mode select used to be written as "own vehicle" - stated
        on the case page as a decision nobody made, and wrong for anything
        over ~10 m."""
        response = self.client.post(
            self.url, self._post_data(asset_length_m="14.00", mode="")
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(
            self.case.transport_order.mode,
            TransportOrder.Mode.EXTERNAL_CARRIER,
        )

    def test_the_soft_checks_actually_reach_the_staff_member(self):
        """TRP-03/05/01 were written as warnings but wired only into the
        Django admin, so the backoffice saved a 14 m own-vehicle inquiry
        without a word."""
        import datetime

        StandardDepartureDay.objects.create(
            code="PON", name="Ponedjeljak", weekday=0
        )
        DeparturePickupSlot.objects.create(
            date=datetime.date(2027, 6, 7), time=datetime.time(8, 0)
        )
        response = self.client.post(
            self.url,
            self._post_data(
                asset_length_m="14.00",
                mode=TransportOrder.Mode.OWN_VEHICLE,
                # a Saturday, matching no published slot
                preferred_date="2027-06-12",
            ),
            follow=True,
        )
        shown = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("TRP-03", shown)
        self.assertIn("TRP-05", shown)
        self.assertIn("TRP-01 warning", shown)

    def test_correcting_the_length_updates_the_linked_asset(self):
        """The field is prefilled from the asset and reads as the asset's
        length, but only ever reached the create branch - so a correction
        left two different lengths on file for one asset."""
        self.client.post(self.url, self._post_data())
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset.length_m, Decimal("6.50"))

        self.client.post(self.url, self._post_data(asset_length_m="7.25"))
        self.case.refresh_from_db()
        self.case.asset.refresh_from_db()
        self.assertEqual(self.case.asset.length_m, Decimal("7.25"))
        self.assertEqual(
            self.case.transport_order.asset_length_m, Decimal("7.25")
        )


class TransportPricingViewTests(TestCase):
    def setUp(self):
        self.row = TransportPriceMatrix.objects.create(
            model_code="M20", destination_zone=DestinationZone.ZONE_A,
            price=Decimal("3400"),
        )
        self.page = reverse("backoffice:section-transport-pricing")
        self.add_url = reverse("backoffice:transport-price-add")

    def _staff(self, role=""):
        user = get_user_model().objects.create_user(
            username="u_%s" % (role or "plain"), password="demo12345",
            is_staff=True,
        )
        if role:
            user.role = role
            user.save()
        return user

    def test_any_staff_can_view(self):
        self.client.force_login(self._staff())
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "M20")

    def test_non_privileged_staff_cannot_add(self):
        self.client.force_login(self._staff())
        self.client.post(
            self.add_url,
            {
                "new-model_code": "M22",
                "new-destination_zone": DestinationZone.ZONE_A,
                "new-price": "3500",
                "new-is_active": "on",
            },
            follow=True,
        )
        self.assertFalse(
            TransportPriceMatrix.objects.filter(model_code="M22").exists()
        )

    def test_manager_can_add(self):
        self.client.force_login(self._staff(role="MANAGER"))
        self.client.post(
            self.add_url,
            {
                "new-model_code": "M22",
                "new-destination_zone": DestinationZone.ZONE_B,
                "new-price": "5000",
                "new-is_active": "on",
            },
        )
        self.assertTrue(
            TransportPriceMatrix.objects.filter(model_code="M22").exists()
        )

    def test_the_same_model_in_another_casing_is_refused(self):
        """``services.price_for`` matches the model with ``__iexact`` and
        takes the first row by pk, so a second spelling would decide the
        price by which row was typed first - with nothing on the page
        saying so. The database constraint is on the raw string and does
        not see it."""
        self.client.force_login(self._staff(role="MANAGER"))
        response = self.client.post(
            self.add_url,
            {
                "new-model_code": "m20",
                "new-destination_zone": DestinationZone.ZONE_A,
                "new-price": "9999",
                "new-is_active": "on",
            },
            follow=True,
        )
        self.assertContains(response, "already priced for this zone")
        self.assertContains(response, "M20")
        self.assertEqual(TransportPriceMatrix.objects.count(), 1)

    def test_the_same_model_in_another_zone_is_accepted(self):
        self.client.force_login(self._staff(role="MANAGER"))
        self.client.post(
            self.add_url,
            {
                "new-model_code": "m20",
                "new-destination_zone": DestinationZone.ZONE_B,
                "new-price": "5200",
                "new-is_active": "on",
            },
        )
        self.assertEqual(TransportPriceMatrix.objects.count(), 2)

    def test_a_row_may_be_saved_without_tripping_over_itself(self):
        self.client.force_login(self._staff(role="MANAGER"))
        prefix = "price-%s" % self.row.pk
        self.client.post(
            reverse("backoffice:transport-price-edit", args=[self.row.pk]),
            {
                "%s-model_code" % prefix: "M20",
                "%s-brand" % prefix: "Acme",
                "%s-destination_zone" % prefix: DestinationZone.ZONE_A,
                "%s-price" % prefix: "3600",
                "%s-is_active" % prefix: "on",
            },
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.price, Decimal("3600"))


class TransportOfferViewTests(TestCase):
    def setUp(self):
        self.statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
        )
        self.staff = get_user_model().objects.create_user(
            username="tr_staff2", password="demo12345", is_staff=True
        )
        self.client.force_login(self.staff)
        # The offer card is inside the transport section, which the work
        # phase renders only for a case that has an order.
        TransportOrder.objects.create(
            case=self.case,
            origin="North depot",
            destination="Zone A",
            mode=TransportOrder.Mode.EXTERNAL_CARRIER,
        )
        self.url = reverse("backoffice:transport-offer", args=[self.case.pk])

    def test_post_creates_draft_quote(self):
        Quote = self.case.quotes.model
        self.client.post(
            self.url,
            {"description": "Loading; transport; unloading.", "amount": "3400"},
        )
        quote = self.case.quotes.get()
        self.assertEqual(quote.status, Quote.Status.DRAFT)
        self.assertEqual(quote.amount, Decimal("3400"))

    def test_a_rejected_estimate_does_not_destroy_the_typed_scope(self):
        """This view still raised ValidationError(form.errors.as_text()),
        so the redirect stashed nothing: the hand-typed scope was replaced
        by the boilerplate, the accordion shut, and the flash named the raw
        field name."""
        scope = "Loading by crane at the depot; transport; unloading at the destination."
        response = self.client.post(
            self.url, {"description": scope, "amount": ""}, follow=True
        )
        self.assertEqual(self.case.quotes.count(), 0)
        self.assertContains(response, scope)
        self.assertContains(response, "is-invalid")
        self.assertEqual(response.context["invalid_anchor"], "acc-transport")
        shown = " ".join(str(m) for m in response.context["messages"])
        self.assertNotIn("* amount", shown)


class TransportAdvanceDerivationViewTests(TestCase):
    """TRP-04 through the UI: sending the offer derives the 50% advance and
    the only thing left for staff is the "paid" confirmation."""

    def setUp(self):
        self.statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
        )
        self.staff = get_user_model().objects.create_user(
            username="tr_staff3", password="demo12345", is_staff=True
        )
        self.client.force_login(self.staff)

    def _create_offer(self, amount="3400"):
        self.client.post(
            reverse("backoffice:transport-offer", args=[self.case.pk]),
            {"description": "Loading; transport; unloading.", "amount": amount},
        )
        return self.case.quotes.order_by("-pk").first()

    def test_sending_the_offer_derives_the_advance(self):
        quote = self._create_offer("3400")
        self.client.post(
            reverse("backoffice:quote-send", args=[self.case.pk, quote.pk])
        )
        advance = self.case.advance_payments.get()
        self.assertEqual(advance.percent, 50)
        self.assertEqual(advance.amount, Decimal("1700.00"))
        self.assertIsNone(advance.paid_at)
        self.assertEqual(advance.quote_id, quote.pk)

    def test_derive_view_repairs_a_case_whose_offer_predates_the_rule(self):
        from apps.quotes.services import send_quote

        quote = self._create_offer("3400")
        send_quote(quote, self.staff)  # service call: no derivation hook
        self.assertFalse(self.case.advance_payments.exists())

        self.client.post(
            reverse("backoffice:transport-advance-derive", args=[self.case.pk])
        )
        advance = self.case.advance_payments.get()
        self.assertEqual(advance.amount, Decimal("1700.00"))

    def test_derive_view_refuses_without_a_sent_offer(self):
        response = self.client.post(
            reverse("backoffice:transport-advance-derive", args=[self.case.pk]),
            follow=True,
        )
        self.assertFalse(self.case.advance_payments.exists())
        self.assertContains(response, "send the transport offer first")

    def test_mark_paid_button_unlocks_the_departure_transition(self):
        from unittest.mock import patch

        quote = self._create_offer("3400")
        self.client.post(
            reverse("backoffice:quote-send", args=[self.case.pk, quote.pk])
        )
        self.case.status = self.statuses["advance_paid"]
        self.case.save(update_fields=["status"])
        advance = self.case.advance_payments.get()

        self.client.post(
            reverse("backoffice:advance-paid", args=[self.case.pk, advance.pk])
        )
        advance.refresh_from_db()
        self.assertIsNotNone(advance.paid_at)

        # QA T-M2: the departure date is agreed before it is scheduled.
        from apps.transport.models import TransportOrder

        TransportOrder.objects.update_or_create(
            case=self.case,
            defaults={
                "origin": "North depot",
                "destination": "Zone A",
                "preferred_date": date.today() + timedelta(days=30),
            },
        )

        with patch("apps.notifications.services.notify_case_transition"):
            self.client.post(
                reverse("backoffice:case-transition", args=[self.case.pk]),
                {"to_status": "departure_scheduled"},
            )
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "departure_scheduled")


class TransportIntakeExistingAssetTests(TestCase):
    """TRP-01 can link an asset already on file instead of creating a twin."""

    def setUp(self):
        self.statuses = build_workflow(CaseType.TRANSPORT, TRANSPORT_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="John Smith",
        )
        self.owner = Client.objects.create(name="John Smith")
        self.asset = Asset.objects.create(
            client=self.owner,
            name="Unit 7",
            asset_type="Generator",
            registration_no="TV-1",
            length_m=Decimal("6.50"),
        )
        staff = get_user_model().objects.create_user(
            username="tr_staff", password="demo12345", is_staff=True
        )
        self.client.force_login(staff)
        self.url = reverse("backoffice:transport-intake", args=[self.case.pk])

    def _post_data(self, **overrides):
        data = {
            "existing_client": "",
            "name": "",
            "phone": "",
            "email": "",
            "existing_asset": "",
            "asset_name": "",
            "asset_type": "",
            "asset_registration": "",
            "asset_brand": "",
            "asset_model": "",
            "asset_length_m": "6.50",
            "origin_site": OTHER_LOCATION,
            "origin_site_other": "EU luka",
            "destination": "Zone A",
            "destination_zone": DestinationZone.ZONE_A,
            "preferred_date": "",
            "pickup_slot": "",
            "mode": TransportOrder.Mode.OWN_VEHICLE,
        }
        data.update(overrides)
        return data

    def test_picking_the_asset_links_it_and_creates_nothing(self):
        self.case.client = self.owner
        self.case.save(update_fields=["client"])
        response = self.client.post(
            self.url, self._post_data(existing_asset=str(self.asset.pk))
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset, self.asset)
        self.assertEqual(Asset.objects.count(), 1)

    def test_fresh_case_pick_settles_client_and_asset_together(self):
        response = self.client.post(
            self.url,
            self._post_data(
                existing_client=str(self.owner.pk), existing_asset=str(self.asset.pk)
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual((self.case.client, self.case.asset), (self.owner, self.asset))
        self.assertEqual(Client.objects.count(), 1)

    def test_case_page_says_where_the_asset_gets_linked(self):
        detail = reverse("backoffice:case-detail", args=[self.case.pk])
        response = self.client.get(detail)
        self.assertContains(response, "transport cannot be offered without one")
        self.assertContains(response, self.url)
        self.case.client, self.case.asset = self.owner, self.asset
        self.case.save(update_fields=["client", "asset"])
        response = self.client.get(detail)
        self.assertNotContains(response, "transport cannot be offered without one")
        self.assertContains(response, "Unit 7")
