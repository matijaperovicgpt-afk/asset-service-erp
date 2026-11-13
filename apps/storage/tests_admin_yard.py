"""Regressions from the 2026-11-04 module code review (storage admin)."""

from decimal import Decimal

from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from apps.cases.models import Case, CaseType, ReportChannel, Status

from .admin import StorageAgreementForm, StoragePositionAdmin, StoragePositionForm
from .models import StorageAgreement, StorageOrientation, StoragePosition, StorageZone
from .services import place_asset


def make_agreement(suffix, length="10", width="4"):
    status, _ = Status.objects.get_or_create(
        case_type=CaseType.STORAGE,
        code="inquiry",
        defaults={"name": "inquiry", "client_visible_name": "inquiry"},
    )
    case = Case.objects.create(
        case_type=CaseType.STORAGE,
        report_channel=ReportChannel.PHONE,
        raw_contact_name="Asset %s" % suffix,
        status=status,
    )
    return StorageAgreement.objects.create(
        case=case, asset_length_m=Decimal(length), asset_width_m=Decimal(width)
    )


class CoordinatePositionAdminTests(TestCase):
    """The admin applies the yard rules (WNT-05) and no longer 500s on a
    coordinate placement that has no hall/row/slot."""

    def setUp(self):
        self.zone = StorageZone.objects.create(
            name="Yard", width_m=Decimal("40"), length_m=Decimal("25")
        )
        self.a = make_agreement("a")
        self.b = make_agreement("b")
        self.placed = place_asset(
            self.a, self.zone, Decimal("0"), Decimal("0"), StorageOrientation.LENGTHWISE
        )

    def _form(self, agreement, x, y):
        return StoragePositionForm(
            data={
                "agreement": agreement.pk,
                "zone": self.zone.pk,
                "pos_x_m": x,
                "pos_y_m": y,
                "orientation": StorageOrientation.LENGTHWISE,
            }
        )

    def test_an_overlapping_coordinate_placement_is_refused(self):
        form = self._form(self.b, "1", "1")
        self.assertFalse(form.is_valid())

    def test_a_free_coordinate_placement_is_accepted(self):
        form = self._form(self.b, "20", "0")
        self.assertTrue(form.is_valid(), form.errors)

    def test_save_model_skips_the_slot_check_for_coordinate_rows(self):
        # A second coordinate position exists (self.placed); saving another
        # one used to raise "Slot // is already occupied".
        second = StoragePosition(
            agreement=self.b,
            zone=self.zone,
            pos_x_m=Decimal("20"),
            pos_y_m=Decimal("0"),
            orientation=StorageOrientation.LENGTHWISE,
        )
        StoragePositionAdmin(StoragePosition, AdminSite()).save_model(
            None, second, None, False
        )
        self.assertIsNotNone(second.pk)


class StorageAgreementAdminFormTests(TestCase):
    """Dimension edits through the admin obey the backoffice rules."""

    def setUp(self):
        self.agreement = make_agreement("a")

    def _form(self, **changes):
        data = {
            "case": self.agreement.case.pk,
            "asset_length_m": "10",
            "asset_width_m": "4",
            **changes,
        }
        return StorageAgreementForm(data=data, instance=self.agreement)

    def test_a_beam_wider_than_the_outline_is_refused(self):
        self.assertFalse(self._form(asset_width_m="12").is_valid())

    def test_an_implausible_length_is_refused(self):
        self.assertFalse(self._form(asset_length_m="80").is_valid())

    def test_a_placed_asset_cannot_outgrow_its_ground(self):
        zone = StorageZone.objects.create(
            name="Yard", width_m=Decimal("12"), length_m=Decimal("12")
        )
        place_asset(
            self.agreement, zone, Decimal("0"), Decimal("0"),
            StorageOrientation.LENGTHWISE,
        )
        self.assertFalse(self._form(asset_length_m="30").is_valid())

    def test_plausible_values_pass(self):
        self.assertTrue(self._form(asset_width_m="3.5").is_valid())
