"""Tests for the service price list page and the quote
pre-fill from catalog items (official 01.10.2025 price list; ACT-02:
suggestions only)."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel, Status
from apps.cases.tests import build_service_workflow
from apps.quotes.models import Quote
from apps.service.models import ServicePriceItem


def make_item(name, price, category="OPERATION", **kwargs):
    return ServicePriceItem.objects.create(
        category=category, name=name, price=Decimal(price), **kwargs
    )


class ServicePricingViewTests(TestCase):
    """The service price list page: any staff may view, only
    MANAGER/DIRECTOR may change (the StoragePricingViewTests twin)."""

    def setUp(self):
        self.item = make_item("Zamena elise", "60")
        User = get_user_model()
        self.frontdesk = User.objects.create_user(
            username="svcfront", password="demo12345",
            is_staff=True, role="FRONT_DESK",
        )
        self.manager = User.objects.create_user(
            username="svcmgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.page = reverse("backoffice:section-service-pricing")

    def test_any_staff_views_but_cannot_edit(self):
        self.client.force_login(self.frontdesk)
        response = self.client.get(self.page)
        self.assertContains(response, "Zamena elise")
        self.assertNotContains(response, "item-form-new")
        self.assertContains(response, "Only a manager or director")

    def test_frontdesk_change_is_refused(self):
        self.client.force_login(self.frontdesk)
        response = self.client.post(
            reverse("backoffice:pricing-item-add"),
            {"new-category": "OPERATION", "new-name": "Pump replacement",
             "new-price": "60", "new-unit": "FLAT", "new-is_active": "on",
             "new-sort_order": "0"},
            follow=True,
        )
        self.assertContains(response, "may not edit")
        self.assertEqual(ServicePriceItem.objects.count(), 1)

    def test_manager_add_edit_delete(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:pricing-item-add"),
            {"new-category": "CALLOUT", "new-name": "Call-out — North",
             "new-price": "60", "new-unit": "FLAT", "new-is_active": "on",
             "new-sort_order": "0"},
            follow=True,
        )
        self.assertContains(response, "added")
        new_item = ServicePriceItem.objects.get(category="CALLOUT")

        prefix = "item-%s" % self.item.pk
        response = self.client.post(
            reverse("backoffice:pricing-item-edit", args=[self.item.pk]),
            {
                "%s-category" % prefix: "OPERATION",
                "%s-name" % prefix: "Zamena elise",
                "%s-price" % prefix: "70",
                "%s-unit" % prefix: "FLAT",
                "%s-is_active" % prefix: "on",
                "%s-sort_order" % prefix: "3",
            },
            follow=True,
        )
        self.assertContains(response, "updated")
        self.item.refresh_from_db()
        self.assertEqual(self.item.price, Decimal("70"))

        response = self.client.post(
            reverse("backoffice:pricing-item-delete", args=[new_item.pk]),
            follow=True,
        )
        self.assertContains(response, "deleted")
        self.assertEqual(ServicePriceItem.objects.count(), 1)

    def test_duplicate_name_in_category_surfaces_error(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("backoffice:pricing-item-add"),
            {"new-category": "OPERATION", "new-name": "Zamena elise",
             "new-price": "99", "new-unit": "FLAT", "new-is_active": "on",
             "new-sort_order": "0"},
            follow=True,
        )
        self.assertEqual(ServicePriceItem.objects.count(), 1)
        self.assertContains(response, "already exists")


class QuoteCatalogPrefillTests(TestCase):
    """SERVICE quotes may be drafted from price-list items: the sum
    pre-fills the amount, a typed amount always wins (ACT-02), and the
    suggestion is logged for the audit trail."""

    def setUp(self):
        build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        self.elisa = make_item("Zamena elise", "60")
        self.impeler = make_item("Zamena impelera (Alpha I)", "120")
        self.inactive = make_item("Stara operacija", "10", is_active=False)
        self.user = get_user_model().objects.create_user(
            username="svcquote", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse("backoffice:quote-create", args=[self.case.pk])

    def test_items_prefill_amount_description_and_log(self):
        response = self.client.post(
            self.url,
            {
                "catalog_items": [self.elisa.pk, self.impeler.pk],
                "description": "",
                "amount": "",
                "valid_until": "",
            },
            follow=True,
        )
        self.assertContains(response, "created")
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("180"))
        self.assertIn("Zamena elise", quote.description)
        self.assertIn("Zamena impelera (Alpha I)", quote.description)
        log = ActivityLog.objects.filter(
            case=self.case, kind=ActivityLog.Kind.SYSTEM
        ).latest("created_at", "pk")
        self.assertIn("price-list items", log.text)
        self.assertIn("180", log.text)

    def test_typed_amount_wins_over_suggestion(self):
        self.client.post(
            self.url,
            {
                "catalog_items": [self.elisa.pk, self.impeler.pk],
                "description": "Agreed by phone",
                "amount": "150",
                "valid_until": "",
            },
            follow=True,
        )
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("150"))
        self.assertEqual(quote.description, "Agreed by phone")
        log = ActivityLog.objects.filter(
            case=self.case, kind=ActivityLog.Kind.SYSTEM
        ).latest("created_at", "pk")
        self.assertIn("suggested total 180", log.text)
        self.assertIn("final amount 150", log.text)

    def test_neither_amount_nor_items_is_rejected(self):
        response = self.client.post(
            self.url,
            {"catalog_items": [], "description": "x", "amount": "",
             "valid_until": ""},
            follow=True,
        )
        self.assertContains(response, "Enter an amount")
        self.assertFalse(Quote.objects.filter(case=self.case).exists())

    def test_inactive_items_are_not_offered(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, "Zamena elise")
        self.assertNotContains(response, "Stara operacija")

    def test_manual_quote_without_items_still_works(self):
        self.client.post(
            self.url,
            {"description": "Manual quote", "amount": "99.50",
             "valid_until": ""},
            follow=True,
        )
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("99.50"))

    def test_non_service_case_has_no_catalog_field(self):
        from apps.cases.tests import build_workflow

        statuses = build_workflow(
            CaseType.STORAGE, ["inquiry", "quote_sent"]
        )
        storage_case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=statuses["inquiry"],
        )
        response = self.client.get(
            reverse("backoffice:case-detail", args=[storage_case.pk])
        )
        self.assertNotContains(response, 'name="catalog_items"')


class QuoteCatalogRenderTests(TestCase):
    """The price-list picker's markup.

    Passing ``class="form-check-input"`` at widget level made Django copy
    it onto the group's container <div> as well, and Bootstrap's
    ``width:1em;height:1em`` collapsed the whole list into a checkbox-
    sized box that painted over the rest of the card. The class belongs
    on the options only.
    """

    def setUp(self):
        build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        self.elisa = make_item("Zamena elise", "60")
        self.user = get_user_model().objects.create_user(
            username="svcrender", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse("backoffice:case-detail", args=[self.case.pk])

    def test_group_container_is_not_styled_as_a_checkbox(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'class="price-item-list"')
        self.assertNotContains(
            response, 'id="id_catalog_items" class="form-check-input"'
        )

    def test_each_option_is_a_bootstrap_form_check(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'class="form-check price-item-row"')
        self.assertContains(response, 'class="form-check-label"')

    def test_option_carries_the_price_for_client_side_totalling(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'data-price="60.00"')

    def test_price_is_not_localised_into_the_data_attribute(self):
        """The test settings force LANGUAGE_CODE=en, but production runs
        sr-latn: a localised "60,00" would be read as 60 by parseFloat."""
        response = self.client.get(self.url, headers={"accept-language": "sr-latn"})
        self.assertContains(response, 'data-price="60.00"')
        self.assertNotContains(response, 'data-price="60,00"')


class QuoteCatalogHourlyTests(TestCase):
    """A PER_HOUR item is a rate: it is quoted as rate x hours, not as a
    bare rate standing in for an unstated single hour."""

    def setUp(self):
        build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        self.labor = make_item(
            "Radni sat", "50", category="LABOR",
            unit=ServicePriceItem.Unit.PER_HOUR,
        )
        self.elisa = make_item("Zamena elise", "60")
        self.user = get_user_model().objects.create_user(
            username="svchours", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse("backoffice:quote-create", args=[self.case.pk])

    def _post(self, **extra):
        data = {
            "catalog_items": [self.labor.pk],
            "description": "",
            "amount": "",
            "valid_until": "",
        }
        data.update(extra)
        return self.client.post(self.url, data, follow=True)

    def test_hours_multiply_the_rate(self):
        self._post(**{"hours_%s" % self.labor.pk: "2.5"})
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("125.00"))
        self.assertIn("2.5 h", quote.description)
        self.assertIn("50.00", quote.description)

    def test_blank_hours_default_to_one(self):
        self._post(**{"hours_%s" % self.labor.pk: ""})
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("50.00"))
        self.assertIn("1 h", quote.description)

    def test_hours_apply_only_to_the_hourly_item(self):
        self._post(
            catalog_items=[self.labor.pk, self.elisa.pk],
            **{"hours_%s" % self.labor.pk: "3"},
        )
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("210.00"))

    def test_audit_log_matches_the_quote(self):
        self._post(**{"hours_%s" % self.labor.pk: "2"})
        log = ActivityLog.objects.filter(
            case=self.case, kind=ActivityLog.Kind.SYSTEM
        ).latest("created_at", "pk")
        self.assertIn("suggested total 100.00", log.text)
        self.assertIn("final amount 100.00", log.text)

    def test_typed_amount_still_wins(self):
        self._post(amount="80", description="Agreed",
                   **{"hours_%s" % self.labor.pk: "2.5"})
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("80"))

    def test_hours_input_is_rendered_beside_the_hourly_item_only(self):
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertContains(response, 'name="hours_%s"' % self.labor.pk)
        self.assertNotContains(response, 'name="hours_%s"' % self.elisa.pk)
        self.assertContains(response, 'data-hourly="1"')


class PriceFloorTests(TestCase):
    """Money never runs below zero on any of the four configuration
    surfaces.

    The floor lives on the models (``MinValueValidator(0)``), so it is one
    decision applied four times rather than four page-level rules; these
    tests state it once per page, through the page. Zero stays legal - a
    line offered free is not the same as no line at all.
    """

    def setUp(self):
        User = get_user_model()
        self.manager = User.objects.create_user(
            username="floormgr", password="demo12345",
            is_staff=True, role="MANAGER",
        )
        self.client.force_login(self.manager)

    # ------------------------------------------------ storage bands
    def test_negative_band_price_is_refused(self):
        from apps.storage.models import StoragePriceBand

        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "OPEN", "new-length_min_m": "0",
             "new-length_max_m": "14", "new-seasonal_price": "-500"},
            follow=True,
        )
        self.assertContains(response, "greater than or equal to 0")
        self.assertEqual(StoragePriceBand.objects.count(), 0)

    def test_zero_band_price_is_accepted(self):
        from apps.storage.models import StoragePriceBand

        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "OPEN", "new-length_min_m": "0",
             "new-length_max_m": "14", "new-daily_price": "0"},
            follow=True,
        )
        self.assertContains(response, "added")
        band = StoragePriceBand.objects.get()
        self.assertEqual(band.daily_price, Decimal("0"))
        # NULL keeps its own meaning: not offered at all.
        self.assertIsNone(band.seasonal_price)

    def test_negative_band_length_is_refused(self):
        from apps.storage.models import StoragePriceBand

        response = self.client.post(
            reverse("backoffice:pricing-band-add"),
            {"new-accommodation": "OPEN", "new-length_min_m": "-99",
             "new-length_max_m": "-1", "new-daily_price": "10"},
            follow=True,
        )
        self.assertContains(response, "greater than or equal to 0")
        self.assertEqual(StoragePriceBand.objects.count(), 0)

    # ----------------------------------------------- service price catalogue
    def test_negative_service_price_is_refused(self):
        response = self.client.post(
            reverse("backoffice:pricing-item-add"),
            {"new-category": "OPERATION", "new-name": "Zamena elise",
             "new-price": "-250", "new-unit": "FLAT",
             "new-is_active": "on", "new-sort_order": "0"},
            follow=True,
        )
        self.assertContains(response, "greater than or equal to 0")
        self.assertEqual(ServicePriceItem.objects.count(), 0)

    # ------------------------------------------------ transport price matrix
    def test_negative_transport_price_is_refused(self):
        from apps.transport.models import DestinationZone, TransportPriceMatrix

        response = self.client.post(
            reverse("backoffice:transport-price-add"),
            {"new-brand": "Acme", "new-model_code": "M20",
             "new-destination_zone": DestinationZone.values[0],
             "new-price": "-1200", "new-is_active": "on"},
            follow=True,
        )
        self.assertContains(response, "greater than or equal to 0")
        self.assertEqual(TransportPriceMatrix.objects.count(), 0)

    # ------------------------------------------------------ parts catalogue
    def test_negative_part_unit_price_is_refused(self):
        from apps.parts.models import Part

        response = self.client.post(
            reverse("backoffice:part-add"),
            {"new-code": "IMP-1", "new-name": "Impeller",
             "new-stock_qty": "0", "new-unit_price": "-99"},
            follow=True,
        )
        self.assertContains(response, "greater than or equal to 0")
        self.assertEqual(Part.objects.count(), 0)


class QuoteCatalogFloorTests(TestCase):
    """The amount field declares ``min_value``, but a catalogue total is
    assigned in ``clean()`` - after field validation has run - so the
    floor never saw it. A total below the floor is refused on the items,
    not on the amount box the staff member left empty on purpose."""

    def setUp(self):
        build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=Status.objects.get(
                case_type=CaseType.SERVICE, code="new_report"
            ),
        )
        self.user = get_user_model().objects.create_user(
            username="floorquote", password="demo12345", is_staff=True
        )
        self.client.force_login(self.user)
        self.url = reverse("backoffice:quote-create", args=[self.case.pk])

    def post_items(self, *items):
        return self.client.post(
            self.url,
            {"catalog_items": [item.pk for item in items],
             "description": "", "amount": "", "valid_until": ""},
            follow=True,
        )

    def test_a_negative_item_cannot_draft_a_quote(self):
        # The floor stops such an item being created through the page;
        # written directly here because validators do not run on save, so
        # a row from before the floor - or from a fixture - still can.
        negative = make_item("Popust", "-250")
        response = self.post_items(negative)
        self.assertFalse(Quote.objects.filter(case=self.case).exists())
        self.assertContains(response, "Popust")

    def test_items_totalling_exactly_zero_are_refused(self):
        free_one = make_item("Besplatna provjera", "0")
        free_two = make_item("Besplatno pranje", "0")
        response = self.post_items(free_one, free_two)
        self.assertFalse(Quote.objects.filter(case=self.case).exists())
        self.assertContains(response, "0.00 EUR")

    def test_the_refusal_points_at_the_price_list_not_the_amount_box(self):
        response = self.post_items(make_item("Besplatna provjera", "0"))
        # "Enter an amount" would send the reader to a control they
        # deliberately left empty; the items are what has to change.
        self.assertNotContains(response, "Enter an amount")
        self.assertContains(response, "Check the price list")

    def test_a_typed_amount_still_wins_over_a_zero_total(self):
        free = make_item("Besplatna provjera", "0")
        self.client.post(
            self.url,
            {"catalog_items": [free.pk], "description": "Agreed",
             "amount": "120", "valid_until": ""},
            follow=True,
        )
        quote = Quote.objects.get(case=self.case)
        self.assertEqual(quote.amount, Decimal("120"))

    def test_a_normal_total_is_unaffected(self):
        response = self.post_items(make_item("Zamena elise", "60"))
        self.assertContains(response, "created")
        self.assertEqual(Quote.objects.get(case=self.case).amount, Decimal("60"))
