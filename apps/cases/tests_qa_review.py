"""
Regression tests for the QA review of 2026-09-28
(an internal QA review). Each test names the finding it pins.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import PropertyMock, patch

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.scheduling.workinghours import next_working_slot

from .exceptions import GuardCheckFailed
from .models import Case, CaseType, Status, StatusHistory, StatusTransition
from .services import auto_advance_case
from .tests import WorkflowTestBase, build_service_workflow, build_workflow

WINTER_CODES = [
    "inquiry",
    "contracted",
    "estimate_sent",
    "asset_received",
    "in_storage",
    "ready_for_pickup",
    "closed",
]


class ServiceQuoteGateTests(WorkflowTestBase):
    """SV-C1 / SV-H1 / SV-H2."""

    def setUp(self):
        super().setUp()
        self.statuses = build_service_workflow()
        self.Quote = django_apps.get_model("quotes", "Quote")

    def test_sv_c1_awaiting_part_does_not_skip_the_quote(self):
        case = self._make_case(self.statuses, "awaiting_part")
        self.Quote.objects.create(
            case=case, description="Revizija", amount=Decimal("80"),
            status=self.Quote.Status.DRAFT,
        )
        with self.assertRaisesMessage(GuardCheckFailed, "not accepted"):
            self._transition(case, "in_progress")

    def test_sv_c1_awaiting_part_with_no_quote_is_refused(self):
        case = self._make_case(self.statuses, "awaiting_part")
        with self.assertRaisesMessage(GuardCheckFailed, "no quote"):
            self._transition(case, "in_progress")

    def test_sv_c1_awaiting_part_with_an_approved_quote_proceeds(self):
        from apps.quotes.models import Approval
        from apps.quotes.services import record_approval

        ApprovalLimit = django_apps.get_model("accounts", "ApprovalLimit")
        ApprovalLimit.objects.create(role="TECHNICIAN", max_amount=Decimal("100"))
        case = self._make_case(self.statuses, "awaiting_part")
        quote = self.Quote.objects.create(
            case=case, description="Akumulator", amount=Decimal("50"),
            status=self.Quote.Status.SENT,
        )
        record_approval(quote, Approval.Level.CLIENT, Approval.Decision.APPROVED)
        case = self._transition(case, "in_progress")
        self.assertEqual(case.status.code, "in_progress")

    def test_sv_h1_no_approval_on_a_draft_quote(self):
        from apps.quotes.models import Approval
        from apps.quotes.services import record_approval

        case = self._make_case(self.statuses, "awaiting_approval")
        quote = self.Quote.objects.create(
            case=case, description="Nacrt", amount=Decimal("50"),
        )
        with self.assertRaisesMessage(ValidationError, "not been sent"):
            record_approval(
                quote, Approval.Level.CLIENT, Approval.Decision.APPROVED
            )
        quote.refresh_from_db()
        self.assertEqual(quote.status, self.Quote.Status.DRAFT)

    def _work_order(self, case):
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        return WorkOrder.objects.create(case=case, is_preliminary=False)

    def test_sv_h2_a_pass_from_before_the_control_does_not_count(self):
        TestRun = django_apps.get_model("service", "TestRun")
        case = self._make_case(self.statuses, "quality_check")
        work_order = self._work_order(case)
        TestRun.objects.create(
            work_order=work_order,
            result=TestRun.Result.PASSED,
            fault_cleared=True,
            performed_at=timezone.now() - timedelta(days=5),
        )
        StatusHistory.objects.create(
            case=case,
            from_status=self.statuses["in_progress"],
            to_status=self.statuses["quality_check"],
            changed_at=timezone.now() - timedelta(days=1),
        )
        with self.assertRaisesMessage(GuardCheckFailed, "no quality-control"):
            self._transition(case, "ready_for_handover")

    def test_sv_h2_test_run_refused_before_the_work(self):
        from apps.service.services import record_test_run

        TestRun = django_apps.get_model("service", "TestRun")
        case = self._make_case(self.statuses, "new_report")
        with self.assertRaisesMessage(ValidationError, "SVC-16"):
            record_test_run(
                self._work_order(case),
                TestRun.Result.PASSED,
                self.user,
                kind=TestRun.Kind.TEST_RUN,
                fault_cleared=True,
            )
        self.assertFalse(TestRun.objects.exists())


class WinterStorageAutoAdvanceTests(WorkflowTestBase):
    """S-H1 / S-M1."""

    def setUp(self):
        super().setUp()
        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.case = self._make_case(
            self.statuses, "asset_received", case_type=CaseType.STORAGE
        )

    def test_s_h1_auto_advance_stops_at_asset_received(self):
        self.assertEqual(auto_advance_case(self.case, self.user), [])
        self.case.refresh_from_db()
        self.assertEqual(self.case.status.code, "asset_received")

    def test_s_h1_in_storage_needs_a_placement(self):
        with self.assertRaisesMessage(GuardCheckFailed, "WNT-05"):
            self._transition(self.case, "in_storage")

        from apps.storage.services import upsert_agreement

        agreement = upsert_agreement(self.case, asset_length_m=Decimal("9"))
        StoragePosition = django_apps.get_model("storage", "StoragePosition")
        StoragePosition.objects.create(agreement=agreement, hall="A", row="1", slot="2")
        case = self._transition(self.case, "in_storage")
        self.assertEqual(case.status.code, "in_storage")

    def test_s_m1_an_offer_from_before_contracting_is_not_the_estimate(self):
        Quote = django_apps.get_model("quotes", "Quote")
        case = self._make_case(
            self.statuses, "contracted", case_type=CaseType.STORAGE
        )
        quote = Quote.objects.create(
            case=case, description="Early quote", amount=Decimal("2200"),
            status=Quote.Status.SENT,
        )
        Quote.objects.filter(pk=quote.pk).update(
            created_at=timezone.now() - timedelta(days=3)
        )
        StatusHistory.objects.create(
            case=case,
            from_status=self.statuses["inquiry"],
            to_status=self.statuses["contracted"],
            changed_at=timezone.now() - timedelta(days=1),
        )
        with self.assertRaisesMessage(GuardCheckFailed, "WNT-03"):
            self._transition(case, "estimate_sent")


class SeedWorkflowQaTests(TestCase):
    """R-M2 / S-H5 / P2 / P4."""

    def _seed(self):
        call_command("seed_workflow", stdout=StringIO())

    def test_r_m2_the_part_detour_is_manager_only(self):
        self._seed()
        edge = StatusTransition.objects.get(
            from_status__case_type=CaseType.SERVICE,
            from_status__code="awaiting_approval",
            to_status__code="awaiting_part",
        )
        self.assertEqual(sorted(edge.allowed_roles), ["DIRECTOR", "MANAGER"])

    def test_s_h5_a_retired_status_nobody_uses_is_pruned(self):
        self._seed()
        inquiry = Status.objects.get(
            case_type=CaseType.STORAGE, code="inquiry"
        )
        legacy = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="quote_sent",
            name="Quote sent",
            client_visible_name="Received",
            sort_order=99,
        )
        StatusTransition.objects.create(from_status=inquiry, to_status=legacy)
        self._seed()
        self.assertFalse(Status.objects.filter(pk=legacy.pk).exists())

    def test_s_h5_a_retired_status_still_in_use_is_kept(self):
        self._seed()
        legacy = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="quote_sent",
            name="Quote sent",
            client_visible_name="Received",
            sort_order=99,
        )
        Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel="PHONE",
            status=legacy,
        )
        self._seed()
        self.assertTrue(Status.objects.filter(pk=legacy.pk).exists())

    def test_p4_status_names_are_english(self):
        self._seed()
        names = set(Status.objects.values_list("name", flat=True))
        self.assertIn("Awaiting part", names)
        self.assertIn("Estimate sent", names)


class ClosedCaseViewTests(TestCase):
    """Root cause 2 (INV-15 outside ``CaseActionView``): S-H2, S-H3,
    T-H3, T-H4, SV-H3. Asserts on the database, not the HTTP status -
    these views redirect whether they accepted or refused."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.user = get_user_model().objects.create_user(
            username="qa", password="demo12345", is_staff=True, role="MANAGER"
        )
        self.client.force_login(self.user)
        self.winter = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.transport = build_workflow(
            CaseType.TRANSPORT,
            ["inquiry", "quote_sent", "advance_paid", "departure_scheduled",
             "in_transit", "delivered", "closed"],
        )
        Client = django_apps.get_model("clients", "Client")
        Asset = django_apps.get_model("clients", "Asset")
        self.owner = Client.objects.create(name="Owner", phone="+44 67 1")
        self.asset = Asset.objects.create(
            client=self.owner, name="Bura", length_m=Decimal("9.50")
        )

    def _storage_case(self, code):
        from apps.storage.services import upsert_agreement

        case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel="PHONE",
            status=self.winter[code],
            client=self.owner,
            asset=self.asset,
        )
        upsert_agreement(case, asset_length_m=Decimal("9.50"))
        return case

    def _zone(self):
        StorageZone = django_apps.get_model("storage", "StorageZone")
        return StorageZone.objects.create(
            name="QA", width_m=Decimal("40"), length_m=Decimal("25")
        )

    def _place(self, case, zone):
        return self.client.post(
            reverse("backoffice:storage-place", args=[case.pk]),
            {"zone": zone.pk, "pos_x_m": "2", "pos_y_m": "2",
             "orientation": "LENGTHWISE"},
        )

    def test_s_h2_closed_case_is_not_placed(self):
        case = self._storage_case("closed")
        self._place(case, self._zone())
        self.assertFalse(case.storage_agreement.positions.exists())

    def test_s_h3_inquiry_is_not_placed(self):
        case = self._storage_case("inquiry")
        self._place(case, self._zone())
        self.assertFalse(case.storage_agreement.positions.exists())

    def test_s_h2_contracted_case_is_placed(self):
        case = self._storage_case("contracted")
        self._place(case, self._zone())
        self.assertTrue(case.storage_agreement.positions.exists())

    def test_s_h2_closed_case_is_not_lifted_out(self):
        from apps.storage.services import place_asset

        case = self._storage_case("contracted")
        place_asset(
            case.storage_agreement, self._zone(), Decimal("2"), Decimal("2")
        )
        case.status = self.winter["closed"]
        case.save(update_fields=["status"])
        self.client.post(reverse("backoffice:storage-unplace", args=[case.pk]))
        self.assertTrue(case.storage_agreement.positions.exists())

    def test_s_h2_closed_case_storage_entry_is_refused(self):
        case = self._storage_case("closed")
        self.client.post(
            reverse("backoffice:storage-intake", args=[case.pk]),
            {"asset_length_m": "14.50", "ownership": "X", "origin_site": "Y"},
        )
        case.storage_agreement.refresh_from_db()
        self.asset.refresh_from_db()
        self.assertEqual(case.storage_agreement.asset_length_m, Decimal("9.50"))
        self.assertEqual(self.asset.length_m, Decimal("9.50"))

    def _transport_case(self, code):
        TransportOrder = django_apps.get_model("transport", "TransportOrder")
        case = Case.objects.create(
            case_type=CaseType.TRANSPORT,
            report_channel="PHONE",
            status=self.transport[code],
            client=self.owner,
            asset=self.asset,
        )
        TransportOrder.objects.create(
            case=case, origin="North depot", destination="Zone A",
            asset_length_m=Decimal("9.50"),
            mode=TransportOrder.Mode.OWN_VEHICLE,
        )
        return case

    def test_t_h3_inquiry_is_frozen_once_departure_is_scheduled(self):
        for code in ("in_transit", "closed"):
            case = self._transport_case(code)
            self.client.post(
                reverse("backoffice:transport-intake", args=[case.pk]),
                {"asset_length_m": "14.50", "mode": "EXTERNAL_CARRIER",
                 "origin_site": "North depot", "destination": "Skopje"},
            )
            case.transport_order.refresh_from_db()
            self.assertEqual(case.transport_order.mode, "OWN_VEHICLE", code)
            self.assertEqual(case.transport_order.destination, "Zone A", code)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.length_m, Decimal("9.50"))

    def test_t_h4_delivered_is_not_stamped_at_inquiry(self):
        case = self._transport_case("inquiry")
        self.client.post(reverse("backoffice:transport-delivered", args=[case.pk]))
        case.transport_order.refresh_from_db()
        self.assertIsNone(case.transport_order.delivered_at)

    def test_sv_h3_triage_does_not_move_a_closed_case(self):
        Client = django_apps.get_model("clients", "Client")
        other = Client.objects.create(name="Drugi")
        case = self._storage_case("closed")
        self.client.post(
            reverse("backoffice:case-triage", args=[case.pk]),
            {"action": "link", "client_id": other.pk},
        )
        case.refresh_from_db()
        self.assertEqual(case.client_id, self.owner.pk)
        self.assertEqual(case.asset_id, self.asset.pk)

    def test_sv_h3_triage_does_not_move_a_case_mid_workflow(self):
        Client = django_apps.get_model("clients", "Client")
        other = Client.objects.create(name="Drugi")
        case = self._storage_case("contracted")
        self.client.post(
            reverse("backoffice:case-triage", args=[case.pk]),
            {"action": "link", "client_id": other.pk},
        )
        case.refresh_from_db()
        self.assertEqual(case.client_id, self.owner.pk)

    def test_sv_h3_triage_still_links_a_new_report(self):
        case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel="PHONE",
            status=self.winter["inquiry"],
        )
        response = self.client.post(
            reverse("backoffice:case-triage", args=[case.pk]),
            {"action": "link", "client_id": self.owner.pk},
        )
        case.refresh_from_db()
        self.assertEqual(case.client_id, self.owner.pk)
        # UI-M3 / SV-L1: back to the case, not the case list.
        self.assertRedirects(
            response,
            reverse("backoffice:case-detail", args=[case.pk]),
            fetch_redirect_response=False,
        )

    def test_ui_m3_triage_refuses_a_negative_length(self):
        Asset = django_apps.get_model("clients", "Asset")
        case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel="PHONE",
            status=self.winter["inquiry"],
        )
        self.client.post(
            reverse("backoffice:case-triage", args=[case.pk]),
            {"action": "create", "name": "Novi", "asset_name": "Minus",
             "asset_length_m": "-1"},
        )
        self.assertFalse(Asset.objects.filter(name="Minus").exists())


class RoleGateTests(WorkflowTestBase):
    """Root cause 5: SV-H4 / R-M1, SV-M7, SV-M8."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model

        User = get_user_model()
        self.tech = User.objects.create_user(
            username="t", password="demo12345", is_staff=True, role="TECHNICIAN"
        )
        self.desk = User.objects.create_user(
            username="d", password="demo12345", is_staff=True, role="FRONT_DESK"
        )
        self.manager = User.objects.create_user(
            username="m", password="demo12345", is_staff=True, role="MANAGER"
        )
        self.statuses = build_service_workflow()
        self.case = self._make_case(self.statuses, "diagnostics")

    def _quick_add(self, user, code):
        self.client.force_login(user)
        self.client.post(
            reverse("backoffice:part-quick-add", args=[self.case.pk]),
            {"code": code, "name": "Filter", "is_consumable": "on",
             "stock_qty": "999", "unit_price": "0"},
        )
        Part = django_apps.get_model("parts", "Part")
        return Part.objects.get(code=code)

    def test_sv_h4_technician_adds_an_ordinary_part(self):
        part = self._quick_add(self.tech, "QA-T")
        self.assertFalse(part.is_consumable)
        self.assertEqual(part.stock_qty, 0)

    def test_sv_h4_manager_may_add_a_stocked_consumable(self):
        part = self._quick_add(self.manager, "QA-M")
        self.assertTrue(part.is_consumable)
        self.assertEqual(part.stock_qty, 999)

    def test_sv_m7_front_desk_does_not_sign_the_handover(self):
        from apps.intake.services import sign_handover_technician

        HandoverRecord = django_apps.get_model("intake", "HandoverRecord")
        handover = HandoverRecord.objects.create(case=self.case)
        with self.assertRaisesMessage(ValidationError, "SVC-18"):
            sign_handover_technician(handover, self.desk)
        handover.refresh_from_db()
        self.assertIsNone(handover.technician_signed_at)
        sign_handover_technician(handover, self.tech)

    def test_sv_m8_technician_does_not_record_a_payment(self):
        from apps.quotes.services import mark_advance_paid

        AdvancePayment = django_apps.get_model("quotes", "AdvancePayment")
        advance = AdvancePayment.objects.create(
            case=self.case, percent=50, amount=Decimal("100")
        )
        with self.assertRaises(ValidationError):
            mark_advance_paid(advance, self.tech)
        advance.refresh_from_db()
        self.assertIsNone(advance.paid_at)
        mark_advance_paid(advance, self.desk)
        self.assertIsNotNone(advance.paid_at)


class StorageMediumTests(WorkflowTestBase):
    """S-H4, S-M2, S-M3, S-M4, S-M5, S-M8."""

    def setUp(self):
        super().setUp()
        from apps.storage.services import upsert_agreement

        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.case = self._make_case(
            self.statuses, "contracted", case_type=CaseType.STORAGE
        )
        self.agreement = upsert_agreement(
            self.case, permit_no="SP-1", asset_length_m=Decimal("8")
        )
        StorageZone = django_apps.get_model("storage", "StorageZone")
        self.zone = StorageZone.objects.create(
            name="QA", width_m=Decimal("20"), length_m=Decimal("20"),
            clearance_m=Decimal("0.5"),
        )

    def _other_asset_at(self, x, y):
        from apps.storage.services import place_asset, upsert_agreement

        other = self._make_case(
            self.statuses, "contracted", case_type=CaseType.STORAGE
        )
        agreement = upsert_agreement(other, asset_length_m=Decimal("8"))
        return place_asset(agreement, self.zone, Decimal(x), Decimal(y))

    def test_s_h4_placed_length_cannot_be_cleared(self):
        from apps.storage.services import place_asset, upsert_agreement

        place_asset(self.agreement, self.zone, Decimal("1"), Decimal("1"))
        with self.assertRaises(ValidationError):
            upsert_agreement(self.case, asset_length_m=None)
        self.agreement.refresh_from_db()
        self.assertEqual(self.agreement.asset_length_m, Decimal("8"))

    def test_s_h4_placed_length_cannot_be_cleared_before_contracting(self):
        from apps.storage.services import place_asset, upsert_agreement

        self.case.status = self.statuses["inquiry"]
        self.case.save(update_fields=["status"])
        place_asset(self.agreement, self.zone, Decimal("1"), Decimal("1"))
        with self.assertRaisesMessage(ValidationError, "WNT-05"):
            upsert_agreement(self.case, asset_length_m=None)

    def test_s_h4_lengthening_into_a_neighbour_is_refused(self):
        from apps.storage.services import place_asset, upsert_agreement

        place_asset(self.agreement, self.zone, Decimal("1"), Decimal("1"))
        self._other_asset_at("1", "11")
        with self.assertRaisesMessage(ValidationError, "no longer fits"):
            upsert_agreement(self.case, asset_length_m=Decimal("12"))
        self.agreement.refresh_from_db()
        self.assertEqual(self.agreement.asset_length_m, Decimal("8"))

    def test_s_m4_implausible_values_refused(self):
        from apps.storage.services import upsert_agreement

        for fields in (
            {"agreed_price": Decimal("-500")},
            {"agreed_price": Decimal("0")},
            {"asset_length_m": Decimal("999.99")},
            {"asset_width_m": Decimal("30")},
            {"permit_no": ""},
        ):
            with self.assertRaises(ValidationError, msg=str(fields)):
                upsert_agreement(self.case, **fields)

    def test_s_m2_offer_period_is_validated(self):
        import datetime

        from apps.storage.services import validate_offer_period

        d = datetime.date
        for start, end in (
            (d(2026, 10, 15), d(2026, 1, 1)),
            (d(2026, 10, 15), d(2226, 10, 15)),
            (d(2026, 10, 15), None),
        ):
            with self.assertRaises(ValidationError):
                validate_offer_period(start, end)
        validate_offer_period(d(2026, 10, 15), d(2027, 10, 15))

    def test_s_m3_offer_near_season_end_is_for_the_next_season(self):
        import datetime

        from apps.storage.services import offer_season

        self.assertEqual(
            offer_season(datetime.date(2026, 9, 28)),
            (datetime.date(2026, 10, 15), datetime.date(2027, 10, 15)),
        )
        self.assertEqual(
            offer_season(datetime.date(2026, 11, 2)),
            (datetime.date(2026, 10, 15), datetime.date(2027, 10, 15)),
        )

    def test_s_m5_closing_releases_the_ground(self):
        from apps.storage.services import place_asset

        place_asset(self.agreement, self.zone, Decimal("1"), Decimal("1"))
        self.case.status = self.statuses["ready_for_pickup"]
        self.case.financially_settled = True
        self.case.save()
        HandoverRecord = django_apps.get_model("intake", "HandoverRecord")
        HandoverRecord.objects.create(case=self.case, signed_at=timezone.now())
        self._transition(self.case, "closed")
        self.assertFalse(self.agreement.positions.exists())

    def test_s_m8_site_pickup_dates_and_carrier(self):
        from apps.backoffice.forms import SitePickupForm

        form = SitePickupForm(
            data={
                "lift_out_date": "2026-10-20",
                "scheduled_date": "2026-10-10",
                "transport_mode": "EXTERNAL_CARRIER",
                "carrier_name": "",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("scheduled_date", form.errors)
        self.assertIn("carrier_name", form.errors)


class TransportAndServiceMediumTests(WorkflowTestBase):
    """T-M3, T-H1 (plan dates), SV-M1, SV-M4, SV-M9, S-L2."""

    def test_t_m3_carrier_price_zero_refused(self):
        from apps.backoffice.forms import TransportCarrierPriceForm as CarrierPriceForm

        form = CarrierPriceForm(data={"carrier_price": "0"})
        self.assertFalse(form.is_valid())

    def test_t_h1_unloading_before_loading_refused(self):
        from apps.backoffice.forms import LoadingPlanForm

        form = LoadingPlanForm(
            data={
                "unloading_location": "Ramp",
                "lifting_availability": "AVAILABLE",
                "attendee": "Owner",
                "loading_at": "2026-01-01T10:00",
                "unloading_at": "2025-12-31T10:00",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("unloading_at", form.errors)

    def test_sv_m9_absurd_quantity_is_a_form_error(self):
        from apps.backoffice.forms import QUANTITY_MAX, IntakeItemForm

        form = IntakeItemForm(
            data={"item_name": "Sidro", "quantity": "99999999999",
                  "category": "OTHER"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("quantity", form.errors)
        self.assertLess(QUANTITY_MAX, 2**31)

    def test_sv_m1_no_intake_on_an_in_water_case(self):
        from apps.intake.services import create_intake_record

        statuses = build_service_workflow()
        case = self._make_case(statuses, "intake_scheduled")
        WorkOrder = django_apps.get_model("service", "WorkOrder")
        WorkOrder.objects.create(case=case, is_preliminary=True)
        with patch.object(
            type(case), "requires_asset_intake",
            new_callable=PropertyMock, return_value=False,
        ):
            with self.assertRaisesMessage(ValidationError, "SVC-03"):
                create_intake_record(
                    case, self.user, mount_status="ON_MOUNT",
                    cleanliness_status="CLEAN", damage_note="none",
                )

    def test_sv_m4_completion_notice_waits_for_the_control(self):
        from apps.service.services import send_completion_notice

        statuses = build_service_workflow()
        case = self._make_case(statuses, "new_report")
        with self.assertRaisesMessage(ValidationError, "passed the control"):
            send_completion_notice(
                case, self.user, works_performed="Service", final_cost=Decimal("100"),
                handover_mode="PICKUP", scheduled_for=next_working_slot(),
                channel="EMAIL", cost_note="negotiable",
            )

    def test_ui_m5_expired_session_on_htmx_redirects_the_whole_page(self):
        statuses = build_service_workflow()
        case = self._make_case(statuses, "new_report")
        response = self.client.post(
            reverse("backoffice:case-activity-log", args=[case.pk]),
            {"text": "x"},
            HTTP_HX_REQUEST="true",
            HTTP_HX_CURRENT_URL="http://testserver/backoffice/cases/%s/" % case.pk,
        )
        self.assertEqual(response.status_code, 204)
        self.assertIn(
            "next=/backoffice/cases/%s/" % case.pk,
            response["HX-Redirect"].replace("%2F", "/"),
        )

    def test_p1_save_all_skips_rows_unchanged_on_the_page(self):
        """QA P1: every row posts its page-load value; a row another
        technician changed meanwhile was reverted by "Save all"."""
        from types import SimpleNamespace

        from apps.backoffice.forms import InspectionResultsForm
        from apps.service.models import InspectionCheckItem

        values = [v for v in InspectionCheckItem.Status.values if v]
        a, b = values[0], values[1]
        items = [
            SimpleNamespace(pk=1, status=a, note=""),
            SimpleNamespace(pk=2, status=a, note=""),
        ]
        form = InspectionResultsForm(
            data={
                "status_1": a, "note_1": "", "orig_status_1": a, "orig_note_1": "",
                "status_2": b, "note_2": "x", "orig_status_2": a, "orig_note_2": "",
            },
            items=items,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.results(), {2: (b, "x")})

    def test_ui_l8_out_of_range_pages_clamp_instead_of_404(self):
        self.client.force_login(self.user)
        for page in ("9999", "abc", "0", ""):
            for name in ("backoffice:case-list", "backoffice:client-list"):
                response = self.client.get(reverse(name) + "?page=" + page)
                self.assertEqual(response.status_code, 200, (name, page))

    def test_s_l2_fit_check_survives_nan(self):
        self.client.force_login(self.user)
        for query in ("length=NaN", "width=sNaN", "length=Infinity"):
            response = self.client.get(
                reverse("backoffice:storage-fit-check") + "?" + query
            )
            self.assertEqual(response.status_code, 200, query)
