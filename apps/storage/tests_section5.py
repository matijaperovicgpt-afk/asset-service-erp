"""Procedure §5 "Storage service procedure" - the gaps the audit found.

Three of the five sub-steps needed nothing: the offer (§5.3) already tells
a renewal apart from a recommended package and runs the 15.10 season, the
site pickup (§5.4) already hangs off the shared intake stack, and the
yard plan (§5.5) already refuses a placement that would trap an asset due
out earlier - on both the grid and the graphic layout.

What was missing was §5.2. The procedure names five things that go into
the database - length, site location, ownership, contact and the
selected services - and three of them were demanded only by the WNT-02
page's form. Nothing routes a case through that page: the agreement card
on the case detail supplies permit, length, accommodation and term all by
itself, so a case could be contracted with the other three blank.
"""
import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.cases.exceptions import GuardCheckFailed
from apps.cases.models import Case, CaseType, ReportChannel
from apps.cases.services import transition_case
from apps.cases.tests import build_workflow
from apps.clients.models import Client, Asset
from apps.intake.models import IntakeRecord
from apps.storage.models import (
    SitePickup,
    StorageAgreement,
    StoragePackage,
    StorageServiceType,
)
from apps.storage.services import (
    attach_permit,
    has_permit,
    record_site_pickup,
    upsert_agreement,
)

WINTER_CODES = ["inquiry", "contracted", "estimate_sent"]


class DatabaseEntryGuardTests(TestCase):
    """§5.2 "Unos u bazu": all five items, checked where the status moves."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="wnt02", password="demo12345", is_staff=True
        )
        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.client_record = Client.objects.create(
            name="Mark", phone="+4469111222"
        )
        self.asset = Asset.objects.create(
            client=self.client_record, name="Bora", length_m=Decimal("8.50")
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
            client=self.client_record,
            asset=self.asset,
        )
        self.service = StorageServiceType.objects.create(name="Storage")

    def _document(self, **overrides):
        fields = {
            "permit_no": "MNE-1234",
            "asset_length_m": Decimal("8.50"),
            "accommodation": "OPEN",
            "term": "SEASONAL",
            "origin_site": "Customer site",
            "ownership": "John Smith",
            "selected_services": [self.service],
        }
        fields.update(overrides)
        return upsert_agreement(self.case, **fields)

    def _contract(self):
        self.case.refresh_from_db()
        return transition_case(self.case, "contracted", self.user)

    def test_the_fully_documented_inquiry_contracts(self):
        self._document()
        self.assertEqual(self._contract().status.code, "contracted")

    def test_the_site_is_required(self):
        self._document(origin_site="")
        with self.assertRaisesMessage(GuardCheckFailed, "site"):
            self._contract()

    def test_the_ownership_is_required(self):
        self._document(ownership="")
        with self.assertRaisesMessage(GuardCheckFailed, "ownership"):
            self._contract()

    def test_at_least_one_service_is_required(self):
        self._document(selected_services=[])
        with self.assertRaisesMessage(GuardCheckFailed, "selected service"):
            self._contract()

    def test_several_services_are_recordable(self):
        """§5.2 says "selected services" - plural. The single foreign key
        this replaced could hold one, so the second and third choice had
        nowhere to go."""
        wash = StorageServiceType.objects.create(name="Pressure wash")
        conservation = StorageServiceType.objects.create(name="Conservation")
        agreement = self._document(
            selected_services=[self.service, wash, conservation]
        )
        self.assertEqual(agreement.selected_services.count(), 3)
        self.assertEqual(self._contract().status.code, "contracted")

    def test_omitting_the_key_leaves_the_selection_alone(self):
        """The incremental contract the scalar fields have: a partial
        phone inquiry is completed, not cleared."""
        self._document()
        agreement = upsert_agreement(self.case, ownership="Jane Smith")
        self.assertEqual(agreement.selected_services.count(), 1)

    def test_passing_an_empty_list_does_clear_it(self):
        """Explicitly saying "none" is different from saying nothing."""
        self._document()
        agreement = upsert_agreement(self.case, selected_services=[])
        self.assertEqual(agreement.selected_services.count(), 0)

    def test_the_log_names_every_service(self):
        from apps.cases.models import ActivityLog
        from apps.storage.services import record_storage_entry

        wash = StorageServiceType.objects.create(name="Pressure wash")
        record_storage_entry(
            self.case,
            user=self.user,
            selected_services=[self.service, wash],
            ownership="John Smith",
        )
        entry = ActivityLog.objects.filter(
            case=self.case, text__contains="WNT-02"
        ).latest("pk")
        self.assertIn("Storage", entry.text)
        self.assertIn("Pressure wash", entry.text)


class AssetPermitTests(TestCase):
    """§5.1: the operating permit itself, not only its number."""

    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="wnt01", password="demo12345", is_staff=True
        )
        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
        )
        self.agreement = StorageAgreement.objects.create(case=self.case)

    def test_no_permit_on_file_to_begin_with(self):
        self.assertFalse(has_permit(self.agreement))

    def test_the_document_is_filed_against_the_agreement(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from apps.cases.models import ActivityLog

        attach_permit(
            self.agreement,
            SimpleUploadedFile("permit.pdf", b"%PDF-1.4 fake"),
            user=self.user,
        )
        self.assertTrue(has_permit(self.agreement))
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="permit"
            ).exists()
        )

    def test_it_does_not_gate_contracting(self):
        """The number is the hard requirement: the scan routinely follows
        the inquiry by days, and refusing to contract over it would stop
        work the client has already agreed to."""
        client = Client.objects.create(name="Mark", phone="+4469111222")
        self.case.client = client
        self.case.asset = Asset.objects.create(
            client=client, name="Bora", length_m=Decimal("8.50")
        )
        self.case.save()
        upsert_agreement(
            self.case,
            permit_no="MNE-1234",
            asset_length_m=Decimal("8.50"),
            accommodation="OPEN",
            term="SEASONAL",
            origin_site="Customer site",
            ownership="John Smith",
            selected_services=[
                StorageServiceType.objects.create(name="Storage")
            ],
        )
        self.case.refresh_from_db()
        case = transition_case(self.case, "contracted", self.user)
        self.assertEqual(case.status.code, "contracted")
        self.assertFalse(has_permit(self.case.storage_agreement))


class PackageSeasonTests(TestCase):
    """§5.3: "Paket je za period 15.10. - 15.10. naredne godine"."""

    def _package(self, start, end):
        package = StoragePackage(
            name="Paket", season_start=start, season_end=end
        )
        package.full_clean()
        return package

    def test_the_procedure_season_validates(self):
        self._package(datetime.date(2026, 10, 15), datetime.date(2027, 10, 15))

    def test_a_wrong_start_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "15 October"):
            self._package(
                datetime.date(2026, 11, 1), datetime.date(2027, 10, 15)
            )

    def test_a_wrong_end_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "15 October"):
            self._package(
                datetime.date(2026, 10, 15), datetime.date(2027, 4, 30)
            )

    def test_a_two_year_span_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "FOLLOWING"):
            self._package(
                datetime.date(2026, 10, 15), datetime.date(2028, 10, 15)
            )


class PickupPaperworkTests(TestCase):
    """§5.4: the pickup happens "with the intake record, condition inventory and photos"."""

    def setUp(self):
        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
        )

    def test_completing_without_the_intake_is_refused(self):
        with self.assertRaisesMessage(ValidationError, "intake record"):
            record_site_pickup(
                self.case, status=SitePickup.Status.COMPLETED
            )

    def test_completing_with_it_stamps_the_time(self):
        record = IntakeRecord.objects.create(case=self.case)
        pickup = record_site_pickup(
            self.case,
            status=SitePickup.Status.COMPLETED,
            intake_record=record,
        )
        self.assertIsNotNone(pickup.completed_at)
        self.assertEqual(pickup.intake_record, record)

    def test_a_pending_pickup_needs_nothing(self):
        """Planning a pickup is not claiming it happened."""
        pickup = record_site_pickup(
            self.case, status=SitePickup.Status.PENDING, carrier_name="Ivan"
        )
        self.assertIsNone(pickup.completed_at)
        self.assertIsNone(pickup.intake_record)
