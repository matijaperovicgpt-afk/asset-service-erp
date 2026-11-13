"""Tests for the storage app (WNT-06 exit-order placement,
OPEN-05 price bands by asset length)."""
import datetime
from decimal import Decimal
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase

from .admin import StorageAgreementAdmin, StoragePositionAdmin
from .models import (
    StorageAgreement,
    StorageOrientation,
    StoragePosition,
    StoragePriceBand,
    StorageZone,
)
from .services import (
    assign_position,
    band_for,
    place_asset,
    price_for,
    validate_band,
)


def build_agreement(code_suffix=""):
    """Create a STORAGE case with a storage agreement."""
    Status = django_apps.get_model("cases", "Status")
    Case = django_apps.get_model("cases", "Case")
    status = Status.objects.create(
        case_type="STORAGE",
        code="new_report%s" % code_suffix,
        name="new_report",
        client_visible_name="new_report",
    )
    case = Case.objects.create(
        case_type="STORAGE",
        report_channel="PHONE",
        status=status,
    )
    return StorageAgreement.objects.create(
        case=case,
        asset_length_m=Decimal("11.50"),
        accommodation="OPEN",
        term="SEASONAL",
    )


class AssignPositionTest(TestCase):
    """WNT-06: higher slot = deeper; deeper assets must leave later."""

    def setUp(self):
        self.agreement_a = build_agreement("a")
        self.agreement_b = build_agreement("b")

    def date(self, month, day, year=2027):
        return datetime.date(year, month, day)

    def test_deeper_asset_leaving_later_is_accepted(self):
        assign_position(self.agreement_a, "H1", "A", 1, self.date(3, 1))
        position = assign_position(self.agreement_b, "H1", "A", 2, self.date(4, 1))
        self.assertIsInstance(position, StoragePosition)
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_equal_exit_dates_are_accepted(self):
        assign_position(self.agreement_a, "H1", "A", 1, self.date(3, 1))
        assign_position(self.agreement_b, "H1", "A", 2, self.date(3, 1))
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_deeper_asset_leaving_earlier_is_rejected(self):
        existing = assign_position(self.agreement_a, "H1", "A", 1, self.date(4, 1))
        with self.assertRaises(ValidationError) as ctx:
            assign_position(self.agreement_b, "H1", "A", 2, self.date(3, 1))
        self.assertIn(str(existing.pk), str(ctx.exception))
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_shallower_asset_leaving_later_is_rejected(self):
        existing = assign_position(self.agreement_a, "H1", "A", 5, self.date(3, 1))
        with self.assertRaises(ValidationError) as ctx:
            assign_position(self.agreement_b, "H1", "A", 2, self.date(4, 1))
        self.assertIn(str(existing.pk), str(ctx.exception))

    def test_null_exit_date_deepest_is_accepted(self):
        # No planned exit = leaves last, so it may sit behind everyone.
        assign_position(self.agreement_a, "H1", "A", 1, self.date(3, 1))
        position = assign_position(self.agreement_b, "H1", "A", 2, None)
        self.assertIsNone(position.planned_exit_date)

    def test_null_exit_date_in_front_of_dated_asset_is_rejected(self):
        # Leaves-last asset parked shallower would trap the dated one.
        existing = assign_position(self.agreement_a, "H1", "A", 5, self.date(3, 1))
        with self.assertRaises(ValidationError) as ctx:
            assign_position(self.agreement_b, "H1", "A", 2, None)
        self.assertIn(str(existing.pk), str(ctx.exception))

    def test_dated_asset_behind_null_exit_date_is_rejected(self):
        assign_position(self.agreement_a, "H1", "A", 1, None)
        with self.assertRaises(ValidationError):
            assign_position(self.agreement_b, "H1", "A", 2, self.date(3, 1))

    def test_both_null_exit_dates_are_accepted(self):
        assign_position(self.agreement_a, "H1", "A", 1, None)
        assign_position(self.agreement_b, "H1", "A", 2, None)
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_other_hall_or_row_does_not_conflict(self):
        assign_position(self.agreement_a, "H1", "A", 5, self.date(3, 1))
        assign_position(self.agreement_b, "H2", "A", 2, self.date(4, 1))
        agreement_c = build_agreement("c")
        assign_position(agreement_c, "H1", "B", 2, self.date(4, 1))
        self.assertEqual(StoragePosition.objects.count(), 3)

    def test_occupied_slot_is_rejected(self):
        existing = assign_position(self.agreement_a, "H1", "A", 3, self.date(3, 1))
        with self.assertRaises(ValidationError) as ctx:
            assign_position(self.agreement_b, "H1", "A", 3, self.date(3, 1))
        self.assertIn(str(existing.pk), str(ctx.exception))

    def test_numeric_slot_ordering_not_lexicographic(self):
        # Slot "10" is deeper than slot "9" even though "10" < "9" as text.
        assign_position(self.agreement_a, "H1", "A", "9", self.date(4, 1))
        with self.assertRaises(ValidationError):
            assign_position(self.agreement_b, "H1", "A", "10", self.date(3, 1))


class StoragePositionAdminTest(TestCase):
    """StoragePosition is registered standalone with WNT-06 in save_model."""

    def setUp(self):
        self.agreement_a = build_agreement("a")
        self.agreement_b = build_agreement("b")
        self.model_admin = StoragePositionAdmin(StoragePosition, AdminSite())
        self.request = RequestFactory().post("/")

    def test_save_model_rejects_trapping_placement(self):
        assign_position(self.agreement_a, "H1", "A", 1, datetime.date(2027, 4, 1))
        obj = StoragePosition(
            agreement=self.agreement_b,
            hall="H1",
            row="A",
            slot="2",
            planned_exit_date=datetime.date(2027, 3, 1),
        )
        with self.assertRaises(ValidationError):
            self.model_admin.save_model(self.request, obj, form=None, change=False)
        self.assertEqual(StoragePosition.objects.count(), 1)

    def test_save_model_accepts_valid_placement(self):
        assign_position(self.agreement_a, "H1", "A", 1, datetime.date(2027, 3, 1))
        obj = StoragePosition(
            agreement=self.agreement_b,
            hall="H1",
            row="A",
            slot="2",
            planned_exit_date=datetime.date(2027, 4, 1),
        )
        self.model_admin.save_model(self.request, obj, form=None, change=False)
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_save_model_allows_editing_existing_position(self):
        # exclude_pk: a position must not conflict with itself on edit.
        position = assign_position(
            self.agreement_a, "H1", "A", 1, datetime.date(2027, 3, 1)
        )
        position.planned_exit_date = datetime.date(2027, 3, 15)
        self.model_admin.save_model(self.request, position, form=None, change=True)
        position.refresh_from_db()
        self.assertEqual(position.planned_exit_date, datetime.date(2027, 3, 15))


class PriceBandTests(TestCase):
    """OPEN-05: storage price matrix - accommodation x length band
    [min, max) x term."""

    def setUp(self):
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("0"),
            length_max_m=Decimal("7"),
            seasonal_price=Decimal("500"),
        )
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("7"),
            length_max_m=Decimal("10"),
            daily_price=Decimal("30"),
            seasonal_price=Decimal("800"),
        )
        # The same range priced independently for the indoor hall.
        self.indoor = StoragePriceBand.objects.create(
            accommodation="INDOOR",
            length_min_m=Decimal("7"),
            length_max_m=Decimal("10"),
            monthly_price=Decimal("400"),
            seasonal_price=Decimal("2700"),
        )
        self.top = StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("15"),
            length_max_m=None,
            seasonal_price=Decimal("2000"),
        )

    def test_min_is_inclusive_max_is_exclusive(self):
        self.assertEqual(
            band_for(Decimal("7.00"), "OPEN").seasonal_price, Decimal("800")
        )
        self.assertEqual(
            band_for(Decimal("6.99"), "OPEN").seasonal_price, Decimal("500")
        )
        self.assertEqual(
            band_for(Decimal("9.99"), "OPEN").seasonal_price, Decimal("800")
        )

    def test_band_is_scoped_to_accommodation(self):
        self.assertEqual(band_for(Decimal("8.00"), "INDOOR"), self.indoor)
        self.assertIsNone(band_for(Decimal("6.00"), "INDOOR"))

    def test_open_ended_top_band(self):
        self.assertEqual(band_for(Decimal("15.00"), "OPEN"), self.top)
        self.assertEqual(band_for(Decimal("42.00"), "OPEN"), self.top)

    def test_gap_yields_no_band(self):
        self.assertIsNone(band_for(Decimal("12.00"), "OPEN"))

    def test_price_for_uses_agreement_length(self):
        agreement = build_agreement()  # 11.50 m OPEN/SEASONAL, in the gap
        self.assertIsNone(price_for(agreement))
        agreement.asset_length_m = Decimal("8.00")
        agreement.save()
        self.assertEqual(price_for(agreement), Decimal("800"))

    def test_price_for_uses_agreement_term(self):
        agreement = build_agreement()
        agreement.asset_length_m = Decimal("8.00")
        agreement.term = "DAILY"
        agreement.save()
        self.assertEqual(price_for(agreement), Decimal("30"))

    def test_unoffered_combination_yields_no_price(self):
        # The OPEN 7-10 band has no monthly price ("-" in the price list).
        agreement = build_agreement()
        agreement.asset_length_m = Decimal("8.00")
        agreement.term = "MONTHLY"
        agreement.save()
        self.assertIsNone(price_for(agreement))

    def test_missing_accommodation_or_term_yields_no_price(self):
        agreement = build_agreement()
        agreement.asset_length_m = Decimal("8.00")
        agreement.accommodation = ""
        agreement.save()
        self.assertIsNone(price_for(agreement))
        agreement.accommodation = "OPEN"
        agreement.term = ""
        agreement.save()
        self.assertIsNone(price_for(agreement))


class ValidateBandTests(TestCase):
    """OPEN-05: bands must be well-formed and non-overlapping within
    one accommodation."""

    def setUp(self):
        self.band = StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("7"),
            length_max_m=Decimal("10"),
            seasonal_price=Decimal("800"),
        )
        self.top = StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("15"),
            length_max_m=None,
            seasonal_price=Decimal("2000"),
        )

    def test_min_at_or_above_max_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_band("OPEN", Decimal("10"), Decimal("10"))
        with self.assertRaises(ValidationError):
            validate_band("OPEN", Decimal("11"), Decimal("10"))

    def test_overlap_names_the_conflicting_band(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_band("OPEN", Decimal("9"), Decimal("12"))
        self.assertIn(str(self.band.pk), str(ctx.exception))

    def test_overlap_with_open_ended_band_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_band("OPEN", Decimal("20"), Decimal("25"))
        with self.assertRaises(ValidationError):
            validate_band("OPEN", Decimal("12"), None)

    def test_same_range_in_other_accommodation_is_accepted(self):
        # Open-yard and indoor-hall bands are independent price tables.
        self.assertIsNone(validate_band("INDOOR", Decimal("7"), Decimal("10")))
        self.assertIsNone(validate_band("INDOOR", Decimal("20"), Decimal("25")))

    def test_adjacent_bands_are_accepted(self):
        self.assertIsNone(validate_band("OPEN", Decimal("10"), Decimal("15")))

    def test_exclude_pk_allows_editing_a_band(self):
        self.assertIsNone(
            validate_band(
                "OPEN", Decimal("7"), Decimal("11"), exclude_pk=self.band.pk
            )
        )


class RecordStorageEntryTests(TestCase):
    """WNT-02 "Unos u bazu": agreement data recorded with an audit log."""

    def _make_case(self, case_type="STORAGE", code="inquiry"):
        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type=case_type,
            code=code,
            name=code,
            client_visible_name=code,
        )
        return Case.objects.create(
            case_type=case_type, report_channel="PHONE", status=status
        )

    def setUp(self):
        from .models import StorageServiceType

        self.case = self._make_case()
        self.service_type = StorageServiceType.objects.create(name="Storage")

    def test_records_agreement_and_wnt02_log(self):
        from .services import record_storage_entry

        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("10"),
            length_max_m=Decimal("15"),
            seasonal_price=Decimal("1200"),
        )
        agreement = record_storage_entry(
            self.case,
            asset_length_m=Decimal("11.50"),
            ownership="Jane Smith",
            origin_site="Customer site",
            selected_services=[self.service_type],
            accommodation="OPEN",
            term="SEASONAL",
        )
        self.assertEqual(agreement.case, self.case)
        self.assertEqual(agreement.ownership, "Jane Smith")
        self.assertEqual(list(agreement.selected_services.all()), [self.service_type])
        self.assertEqual(agreement.accommodation, "OPEN")
        self.assertEqual(agreement.term, "SEASONAL")

        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        log = ActivityLog.objects.get(case=self.case)
        self.assertEqual(log.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("WNT-02", log.text)
        self.assertIn("Jane Smith", log.text)
        self.assertIn("1200", log.text)  # suggested price from the band

    def test_non_storage_case_is_rejected(self):
        from .services import record_storage_entry

        service_case = self._make_case(case_type="SERVICE", code="new_report")
        with self.assertRaises(ValidationError):
            record_storage_entry(service_case, ownership="Jane Smith")
        self.assertFalse(
            StorageAgreement.objects.filter(case=service_case).exists()
        )

    def test_second_call_updates_the_same_agreement(self):
        from .services import record_storage_entry

        record_storage_entry(self.case, permit_no="NP-1")
        record_storage_entry(self.case, ownership="Jane Smith")
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertEqual(StorageAgreement.objects.count(), 1)
        # Incremental completion (upsert): earlier WNT-01 data survives.
        self.assertEqual(agreement.permit_no, "NP-1")
        self.assertEqual(agreement.ownership, "Jane Smith")


class RecordSitePickupTests(TestCase):
    """WNT rule 4 "Pickup from site": pickup logistics recorded on the
    agreement with an audit log; the receipt/inventory/photos stay on the
    shared intake stack via ``intake_record``."""

    def _make_case(self, case_type="STORAGE", code="inquiry"):
        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type=case_type,
            code=code,
            name=code,
            client_visible_name=code,
        )
        return Case.objects.create(
            case_type=case_type, report_channel="PHONE", status=status
        )

    def setUp(self):
        self.case = self._make_case()

    def test_records_pickup_and_rule4_log(self):
        from .models import SitePickup
        from .services import record_site_pickup

        # The site name lives on the agreement (WNT-02).
        StorageAgreement.objects.create(
            case=self.case, origin_site="Customer site"
        )
        pickup = record_site_pickup(
            self.case,
            organized_by=SitePickup.OrganizedBy.SERVICE,
            is_own_site=False,
            lift_out_method=SitePickup.LiftOutMethod.CRANE,
            transport_mode=SitePickup.TransportMode.EXTERNAL_CARRIER,
            carrier_name="Acme Logistics Ltd.",
            status=SitePickup.Status.SCHEDULED,
        )
        self.assertEqual(pickup.agreement.case, self.case)
        self.assertEqual(pickup.lift_out_method, "CRANE")
        self.assertEqual(pickup.carrier_name, "Acme Logistics Ltd.")

        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        log = ActivityLog.objects.filter(case=self.case).latest("id")
        self.assertEqual(log.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("rule 4", log.text)
        self.assertIn("Customer site", log.text)
        self.assertIn("Acme Logistics Ltd.", log.text)

    def test_creates_agreement_when_missing_and_updates_incrementally(self):
        from .models import SitePickup
        from .services import record_site_pickup

        # No agreement yet - record_site_pickup creates one.
        record_site_pickup(self.case, lift_out_method="FORKLIFT")
        record_site_pickup(self.case, carrier_name="Second call")
        self.assertEqual(SitePickup.objects.count(), 1)
        self.assertEqual(StorageAgreement.objects.count(), 1)
        pickup = SitePickup.objects.get()
        # Earlier data survives the second (partial) call.
        self.assertEqual(pickup.lift_out_method, "FORKLIFT")
        self.assertEqual(pickup.carrier_name, "Second call")

    def test_completed_status_stamps_completed_at(self):
        from apps.intake.models import IntakeRecord
        from .models import SitePickup
        from .services import record_site_pickup

        # WNT-05: a completed pickup points at the intake record that
        # documents it - the condition list and the photos hang off it.
        record = IntakeRecord.objects.create(case=self.case)
        pickup = record_site_pickup(
            self.case,
            status=SitePickup.Status.COMPLETED,
            intake_record=record,
        )
        self.assertIsNotNone(pickup.completed_at)
        # A later non-completing call keeps the original timestamp.
        first_stamp = pickup.completed_at
        pickup = record_site_pickup(self.case, carrier_name="x")
        self.assertEqual(pickup.completed_at, first_stamp)

    def test_completing_without_the_intake_is_refused(self):
        """WNT-05 (procedure §5.4): the pickup happens "with the intake record,
        condition inventory and photos" - a pickup reporting itself complete
        while pointing at nothing is claiming paperwork that may not
        exist."""
        from django.core.exceptions import ValidationError

        from .models import SitePickup
        from .services import record_site_pickup

        with self.assertRaisesMessage(ValidationError, "intake record"):
            record_site_pickup(
                self.case, status=SitePickup.Status.COMPLETED
            )

    def test_links_intake_record_as_receipt(self):
        from apps.intake.models import IntakeRecord

        from .services import record_site_pickup

        record = IntakeRecord.objects.create(
            case=self.case, mount_status="OFF_MOUNT", damage_note="none"
        )
        pickup = record_site_pickup(self.case, intake_record=record)
        self.assertEqual(pickup.intake_record, record)

    def test_non_storage_case_is_rejected(self):
        from .models import SitePickup
        from .services import record_site_pickup

        service_case = self._make_case(case_type="SERVICE", code="new_report")
        with self.assertRaises(ValidationError):
            record_site_pickup(service_case, carrier_name="x")
        self.assertFalse(SitePickup.objects.exists())


class StorageAgreementAdminDeviationTests(TestCase):
    """OPEN-05: an agreed price deviating from the suggestion is logged."""

    def setUp(self):
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("10"),
            length_max_m=Decimal("15"),
            seasonal_price=Decimal("1200"),
        )
        # 11.50 m OPEN/SEASONAL -> suggestion 1200
        self.agreement = build_agreement()
        User = django_apps.get_model("accounts", "User")
        self.user = User.objects.create_user(
            username="manager2", password="demo12345", role="MANAGER"
        )

    def _save_via_admin(self):
        model_admin = StorageAgreementAdmin(StorageAgreement, AdminSite())
        request = RequestFactory().post("/")
        request.user = self.user
        with patch("apps.storage.admin.messages.info") as mock_info:
            model_admin.save_model(
                request, self.agreement, form=None, change=True
            )
        return mock_info

    def test_deviating_price_logs_and_messages(self):
        self.agreement.agreed_price = Decimal("1000")
        mock_info = self._save_via_admin()
        mock_info.assert_called_once()
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        entry = ActivityLog.objects.get(case=self.agreement.case)
        self.assertEqual(entry.kind, ActivityLog.Kind.SYSTEM)
        self.assertIn("1000", entry.text)
        self.assertIn("1200", entry.text)

    def test_matching_price_stays_silent(self):
        self.agreement.agreed_price = Decimal("1200")
        mock_info = self._save_via_admin()
        mock_info.assert_not_called()
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_suggested_price_display(self):
        model_admin = StorageAgreementAdmin(StorageAgreement, AdminSite())
        self.assertEqual(
            model_admin.suggested_price(self.agreement), Decimal("1200")
        )


class UpsertAgreementTests(TestCase):
    """WNT-01: incremental documentation writes on the agreement."""

    def _case(self):
        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type="STORAGE",
            code="inquiry",
            name="inquiry",
            client_visible_name="inquiry",
        )
        return Case.objects.create(
            case_type="STORAGE", report_channel="PHONE", status=status
        )

    def test_price_for_returns_none_without_length(self):
        agreement = StorageAgreement.objects.create(
            case=self._case(), accommodation="OPEN", term="SEASONAL"
        )
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("0"),
            length_max_m=None,
            seasonal_price=Decimal("500"),
        )
        self.assertIsNone(price_for(agreement))

    def test_creates_then_updates_incrementally(self):
        from .services import upsert_agreement

        case = self._case()
        agreement = upsert_agreement(case, permit_no="MNE-1")
        self.assertEqual(agreement.permit_no, "MNE-1")
        self.assertIsNone(agreement.asset_length_m)

        again = upsert_agreement(case, asset_length_m=Decimal("9.00"))
        self.assertEqual(again.pk, agreement.pk)  # no duplicate
        self.assertEqual(again.permit_no, "MNE-1")  # untouched
        self.assertEqual(again.asset_length_m, Decimal("9.00"))

    def test_price_deviation_logged_once_relevant(self):
        from .services import record_price_deviation, upsert_agreement

        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        StoragePriceBand.objects.create(
            accommodation="OPEN",
            length_min_m=Decimal("0"),
            length_max_m=None,
            seasonal_price=Decimal("800"),
        )
        case = self._case()
        agreement = upsert_agreement(
            case,
            asset_length_m=Decimal("9.00"),
            accommodation="OPEN",
            term="SEASONAL",
        )

        # Matching price or no agreed price -> nothing to report.
        self.assertIsNone(record_price_deviation(agreement))
        agreement = upsert_agreement(case, agreed_price=Decimal("800"))
        self.assertIsNone(record_price_deviation(agreement))

        agreement = upsert_agreement(case, agreed_price=Decimal("700"))
        note = record_price_deviation(agreement)
        self.assertIn("OPEN-05", note)
        log = ActivityLog.objects.filter(case=case).latest("created_at", "pk")
        self.assertIn("deviates", log.text)


class CurrentSeasonTest(TestCase):
    """WNT-04: seasons run 15 Oct -> 15 Oct next year."""

    def test_date_before_15_oct_belongs_to_previous_season(self):
        from .services import current_season

        start, end = current_season(datetime.date(2026, 8, 13))
        self.assertEqual(start, datetime.date(2025, 10, 15))
        self.assertEqual(end, datetime.date(2026, 10, 15))

    def test_14_oct_still_previous_season(self):
        from .services import current_season

        start, _end = current_season(datetime.date(2026, 10, 14))
        self.assertEqual(start, datetime.date(2025, 10, 15))

    def test_15_oct_starts_the_new_season(self):
        from .services import current_season

        start, end = current_season(datetime.date(2026, 10, 15))
        self.assertEqual(start, datetime.date(2026, 10, 15))
        self.assertEqual(end, datetime.date(2027, 10, 15))


def build_storage_case(code_suffix="", client=None):
    """Create a bare STORAGE case (no agreement)."""
    Status = django_apps.get_model("cases", "Status")
    Case = django_apps.get_model("cases", "Case")
    status = Status.objects.create(
        case_type="STORAGE",
        code="inquiry%s" % code_suffix,
        name="inquiry",
        client_visible_name="inquiry",
    )
    return Case.objects.create(
        case_type="STORAGE",
        report_channel="PHONE",
        status=status,
        client=client,
    )


class PriorPackageAgreementTest(TestCase):
    """WNT-03: an existing package user is detected from earlier storage
    agreements of the same client that carried a package."""

    def setUp(self):
        from .models import StoragePackage

        Client = django_apps.get_model("clients", "Client")
        self.client_obj = Client.objects.create(name="Package Client")
        self.package = StoragePackage.objects.create(
            name="Package deal",
            season_start=datetime.date(2025, 10, 15),
            season_end=datetime.date(2026, 10, 15),
        )

    def test_case_without_client_has_no_prior(self):
        from .services import prior_package_agreement

        case = build_storage_case("x")
        self.assertIsNone(prior_package_agreement(case))

    def test_first_time_client_has_no_prior(self):
        from .services import prior_package_agreement

        old_case = build_storage_case("old", client=self.client_obj)
        StorageAgreement.objects.create(case=old_case)  # no package
        case = build_storage_case("new", client=self.client_obj)
        self.assertIsNone(prior_package_agreement(case))

    def test_existing_package_user_is_detected(self):
        from .services import prior_package_agreement

        old_case = build_storage_case("old", client=self.client_obj)
        prior = StorageAgreement.objects.create(
            case=old_case, package=self.package
        )
        case = build_storage_case("new", client=self.client_obj)
        self.assertEqual(prior_package_agreement(case), prior)

    def test_other_clients_packages_are_ignored(self):
        from .services import prior_package_agreement

        Client = django_apps.get_model("clients", "Client")
        other = Client.objects.create(name="Other Client")
        other_case = build_storage_case("other", client=other)
        StorageAgreement.objects.create(case=other_case, package=self.package)
        case = build_storage_case("new", client=self.client_obj)
        self.assertIsNone(prior_package_agreement(case))

    def test_own_case_is_excluded(self):
        from .services import prior_package_agreement

        case = build_storage_case("self", client=self.client_obj)
        StorageAgreement.objects.create(case=case, package=self.package)
        self.assertIsNone(prior_package_agreement(case))


class SuggestOfferTest(TestCase):
    """WNT-03 prefill: renewal for an existing package user, the package
    as the recommended option for a new client."""

    def setUp(self):
        from .models import PackageComponent, StoragePackage

        Client = django_apps.get_model("clients", "Client")
        self.client_obj = Client.objects.create(name="Client")
        self.package = StoragePackage.objects.create(
            name="Package deal",
            season_start=datetime.date(2025, 10, 15),
            season_end=datetime.date(2026, 10, 15),
        )
        self.component = PackageComponent.objects.create(
            name="Storage", sort_order=0
        )
        self.package.components.add(self.component)

    def test_new_client_gets_package_as_recommended_option(self):
        from .services import suggest_offer

        case = build_storage_case("n", client=self.client_obj)
        offer = suggest_offer(case)
        self.assertFalse(offer["is_renewal"])
        self.assertEqual(offer["package"], self.package)
        self.assertIn("recommended option", offer["description"])
        self.assertIn("Storage", offer["description"])

    def test_existing_package_user_gets_renewal(self):
        from .services import suggest_offer

        old_case = build_storage_case("o", client=self.client_obj)
        prior = StorageAgreement.objects.create(
            case=old_case, package=self.package
        )
        case = build_storage_case("r", client=self.client_obj)
        offer = suggest_offer(case)
        self.assertTrue(offer["is_renewal"])
        self.assertEqual(offer["prior_agreement"], prior)
        self.assertIn("Renewal", offer["description"])

    def test_no_packages_means_no_description(self):
        from .models import StoragePackage
        from .services import suggest_offer

        StoragePackage.objects.all().delete()
        case = build_storage_case("e")
        offer = suggest_offer(case)
        self.assertIsNone(offer["package"])
        self.assertEqual(offer["description"], "")

    def test_agreed_price_prefills_amount(self):
        from .services import suggest_offer, upsert_agreement

        case = build_storage_case("p", client=self.client_obj)
        upsert_agreement(case, agreed_price=Decimal("1500"))
        offer = suggest_offer(case)
        self.assertEqual(offer["amount"], Decimal("1500"))


class CreateStorageOfferTest(TestCase):
    """WNT-03 "Offer": the offer quote for a standalone service or a
    package arrangement, with the commercial period stated in the text."""

    def setUp(self):
        from .models import StoragePackage

        Client = django_apps.get_model("clients", "Client")
        self.client_obj = Client.objects.create(name="Client")
        self.package = StoragePackage.objects.create(
            name="Package deal",
            season_start=datetime.date(2025, 10, 15),
            season_end=datetime.date(2026, 10, 15),
        )
        self.case = build_storage_case(client=self.client_obj)

    def _log_texts(self, case):
        ActivityLog = django_apps.get_model("cases", "ActivityLog")
        return " ".join(
            log.text for log in ActivityLog.objects.filter(case=case)
        )

    def test_package_offer_creates_quote_and_records_choice(self):
        from .services import create_storage_offer
        from .services import offer_season as current_season

        quote = create_storage_offer(
            self.case,
            offer_type="package",
            description="Package deal.",
            amount=Decimal("4000"),
            package=self.package,
        )
        self.assertEqual(quote.status, "draft")
        self.assertIn("Commercial period:", quote.description)
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertEqual(agreement.package, self.package)
        start, end = current_season()
        self.assertEqual(agreement.period_start, start)
        self.assertEqual(agreement.period_end, end)
        self.assertFalse(agreement.is_renewal)
        texts = self._log_texts(self.case)
        self.assertIn("WNT-03: package offer", texts)
        self.assertIn("recommended to a new client", texts)

    def test_package_offer_for_existing_user_is_renewal(self):
        from .services import create_storage_offer

        old_case = build_storage_case("old", client=self.client_obj)
        prior = StorageAgreement.objects.create(
            case=old_case, package=self.package
        )
        create_storage_offer(
            self.case,
            offer_type="package",
            description="Extension.",
            amount=Decimal("4000"),
            package=self.package,
        )
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertTrue(agreement.is_renewal)
        self.assertEqual(agreement.prior_agreement, prior)
        self.assertIn(
            "Renewal of package agreement #%s" % prior.pk,
            self._log_texts(self.case),
        )

    def test_standalone_offer_clears_package(self):
        from .services import create_storage_offer, upsert_agreement

        upsert_agreement(self.case, package=self.package)
        create_storage_offer(
            self.case,
            offer_type="standalone",
            description="Samo storage.",
            amount=Decimal("800"),
            period_start=datetime.date(2026, 10, 15),
            period_end=datetime.date(2027, 10, 15),
        )
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertIsNone(agreement.package)
        self.assertFalse(agreement.is_renewal)
        self.assertEqual(agreement.period_start, datetime.date(2026, 10, 15))
        self.assertIn(
            "standalone storage offer", self._log_texts(self.case)
        )

    def test_package_offer_without_package_is_rejected(self):
        from .services import create_storage_offer

        with self.assertRaises(ValidationError):
            create_storage_offer(
                self.case,
                offer_type="package",
                description="x",
                amount=Decimal("1"),
            )

    def test_unknown_offer_type_is_rejected(self):
        from .services import create_storage_offer

        with self.assertRaises(ValidationError):
            create_storage_offer(
                self.case,
                offer_type="mystery",
                description="x",
                amount=Decimal("1"),
            )

    def test_non_storage_case_is_rejected(self):
        from .services import create_storage_offer

        Status = django_apps.get_model("cases", "Status")
        Case = django_apps.get_model("cases", "Case")
        status = Status.objects.create(
            case_type="SERVICE",
            code="new_report_svc",
            name="new_report",
            client_visible_name="new_report",
        )
        service_case = Case.objects.create(
            case_type="SERVICE", report_channel="PHONE", status=status
        )
        with self.assertRaises(ValidationError):
            create_storage_offer(
                service_case,
                offer_type="standalone",
                description="x",
                amount=Decimal("1"),
            )


class FreePlacementTest(TestCase):
    """WNT-05: to-scale coordinate placement on the graphic yard - bounds,
    collisions and the 2D exit-order rule."""

    def setUp(self):
        self.zone = StorageZone.objects.create(
            name="Yard", width_m=Decimal("40"), length_m=Decimal("25")
        )
        self.a = build_agreement("a")
        self._dims(self.a, 10, 4)
        self.b = build_agreement("b")
        self._dims(self.b, 10, 4)

    def _dims(self, agreement, length, width):
        agreement.asset_length_m = Decimal(str(length))
        agreement.asset_width_m = Decimal(str(width))
        agreement.save()

    def date(self, month, day, year=2027):
        return datetime.date(year, month, day)

    def place(self, agreement, x, y, exit_date, orientation=StorageOrientation.LENGTHWISE):
        return place_asset(
            agreement,
            self.zone,
            Decimal(str(x)),
            Decimal(str(y)),
            orientation,
            exit_date,
        )

    def test_places_asset_to_scale(self):
        pos = self.place(self.a, 0, 0, self.date(3, 1))
        self.assertTrue(pos.is_coordinate_placed)
        self.assertEqual(pos.zone, self.zone)
        self.assertEqual(pos.pos_x_m, Decimal("0"))

    def test_out_of_bounds_is_rejected(self):
        # x=38 with a 4 m breadth reaches 42 m, past the 40 m edge.
        with self.assertRaises(ValidationError):
            self.place(self.a, 38, 0, None)

    def test_overlapping_footprint_is_rejected(self):
        self.place(self.a, 0, 0, self.date(3, 1))
        with self.assertRaises(ValidationError):
            self.place(self.b, 2, 2, self.date(3, 1))

    def test_side_by_side_is_accepted(self):
        self.place(self.a, 0, 0, self.date(3, 1))
        self.place(self.b, 10, 0, self.date(3, 1))
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_blocking_asset_in_front_is_rejected(self):
        # A parked deep (y=10) leaving earlier; B in front (y=0), same lane,
        # leaving later would trap A.
        self.place(self.a, 0, 10, self.date(3, 1))
        with self.assertRaises(ValidationError):
            self.place(self.b, 0, 0, self.date(4, 1))

    def test_clear_lane_is_accepted(self):
        # B in front leaves earlier than the deeper A - reachable, so fine.
        # A starts at y=11, not y=10: the two outlines used to be written
        # flush (B ending exactly where A began), which the yard now
        # refuses because it keeps working space between outlines. A metre
        # apart is what the arrangement was always meant to describe.
        self.place(self.a, 0, 11, self.date(4, 1))
        self.place(self.b, 0, 0, self.date(3, 1))
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_other_lane_ignores_exit_order(self):
        # No X-overlap: B beside A may leave later even parked in front.
        self.place(self.a, 0, 10, self.date(3, 1))
        self.place(self.b, 10, 0, self.date(4, 1))
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_place_asset_upserts_single_position(self):
        self.place(self.a, 0, 0, self.date(3, 1))
        self.place(self.a, 5, 0, self.date(3, 1))
        positions = self.a.positions.filter(zone__isnull=False)
        self.assertEqual(positions.count(), 1)
        self.assertEqual(positions.get().pos_x_m, Decimal("5"))

    def test_missing_length_is_rejected(self):
        self.a.asset_length_m = None
        self.a.save()
        with self.assertRaises(ValidationError):
            self.place(self.a, 0, 0, None)

    def test_length_less_neighbour_does_not_block_another_asset(self):
        """Regression (WNT-05): a placed asset whose length was later
        cleared has no footprint, and must not veto placing a *different*
        asset elsewhere in the zone with an error about a case the user is
        not working on."""
        self.place(self.a, 0, 0, self.date(3, 1))
        self.a.asset_length_m = None
        self.a.save()

        position = self.place(self.b, 20, 0, self.date(4, 1))

        self.assertTrue(position.is_coordinate_placed)
        self.assertEqual(position.agreement_id, self.b.pk)

    def test_length_less_neighbour_is_skipped_not_collision_tested(self):
        """The documented trade-off of skipping: an unmeasured neighbour
        cannot be collision-tested, so its ground is treated as free.
        Recording its length restores the check."""
        self.place(self.a, 0, 0, self.date(3, 1))
        self.a.asset_length_m = None
        self.a.save()

        # Straight onto A's old footprint - accepted while A has no size.
        self.place(self.b, 0, 0, self.date(3, 1))
        self.assertEqual(StoragePosition.objects.count(), 2)

    def test_length_less_neighbour_does_not_block_exit_order_check(self):
        """Skipping the unmeasured neighbour must not swallow the exit-order
        rule for the assets that *are* measured."""
        c = build_agreement("c")
        self._dims(c, 10, 4)
        self.a.asset_length_m = None
        self.a.save()
        # B parked deep, leaving early; C in front leaving later traps it.
        self.place(self.b, 0, 10, self.date(3, 1))
        with self.assertRaises(ValidationError):
            self.place(c, 0, 0, self.date(4, 1))


class AssetFootprintSizeTest(TestCase):
    """WNT-05: the breadth used to draw an asset - recorded when known, an
    estimate from the length otherwise, always in whole centimetres."""

    def setUp(self):
        self.agreement = build_agreement("fp")

    def size(self, length, width=None):
        from .services import asset_footprint_size

        self.agreement.asset_length_m = (
            Decimal(str(length)) if length is not None else None
        )
        self.agreement.asset_width_m = (
            Decimal(str(width)) if width is not None else None
        )
        self.agreement.save()
        return asset_footprint_size(self.agreement)

    def test_recorded_beam_is_returned_untouched(self):
        length, width = self.size("11.50", "4.25")
        self.assertEqual(length, Decimal("11.50"))
        self.assertEqual(width, Decimal("4.25"))

    def test_fallback_beam_is_quantized_to_centimetres(self):
        # 11.50 / 3 = 3.8333... - Decimal keeps every digit unless told not to.
        _length, width = self.size("11.50")
        self.assertEqual(width, Decimal("3.83"))
        self.assertEqual(width.as_tuple().exponent, -2)

    def test_fallback_beam_keeps_the_minimum_floor(self):
        _length, width = self.size("3")
        self.assertEqual(width, Decimal("1.5"))

    def test_zero_recorded_beam_falls_back(self):
        _length, width = self.size("20", "0")
        self.assertEqual(width, Decimal("6.67"))

    def test_quantized_beam_keeps_bounds_errors_readable(self):
        """The fallback width is printed in the out-of-bounds message; full
        Decimal precision made that message unreadable."""
        zone = StorageZone.objects.create(
            name="Yard fp", width_m=Decimal("30"), length_m=Decimal("30")
        )
        self.size("11.50")
        with self.assertRaises(ValidationError) as caught:
            place_asset(
                self.agreement,
                zone,
                Decimal("28"),
                Decimal("0"),
                StorageOrientation.LENGTHWISE,
                None,
            )
        message = str(caught.exception)
        self.assertIn("31.83", message)
        self.assertNotIn("3333", message)


class LocalDateDefaultsTest(TestCase):
    """The module's notion of "today" is ``timezone.localdate()`` (today in
    ``TIME_ZONE``), never the server's ``date.today()`` - on a UTC-hosted
    server the two differ between 00:00 and 02:00 Europe/Belgrade time."""

    def test_current_season_follows_localdate(self):
        from .services import current_season

        with patch(
            "django.utils.timezone.localdate",
            return_value=datetime.date(2030, 11, 20),
        ):
            start, end = current_season()

        self.assertEqual(start, datetime.date(2030, 10, 15))
        self.assertEqual(end, datetime.date(2031, 10, 15))

    def test_current_season_localdate_before_15_oct(self):
        from .services import current_season

        with patch(
            "django.utils.timezone.localdate",
            return_value=datetime.date(2030, 10, 14),
        ):
            start, _end = current_season()

        self.assertEqual(start, datetime.date(2029, 10, 15))

    def test_due_set_window_is_measured_from_localdate(self):
        from .services import agreements_due_for_expiry_reminder

        today = datetime.date(2030, 6, 1)
        inside = self.with_period_end("in", today + datetime.timedelta(days=10))
        self.with_period_end("out", today + datetime.timedelta(days=40))
        self.with_period_end("past", today - datetime.timedelta(days=1))

        with patch("django.utils.timezone.localdate", return_value=today):
            due = list(agreements_due_for_expiry_reminder())

        self.assertEqual([a.pk for a in due], [inside.pk])

    def with_period_end(self, suffix, period_end):
        agreement = build_agreement(suffix)
        agreement.period_end = period_end
        agreement.save()
        return agreement
