"""Tests for the clients app: models and triage matching/linking services."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel, Status

from .models import Client, Asset
from .services import link_case, normalize_name, normalize_phone, suggest_matches


def identified_asset(client=None, **overrides):
    """§7.2: an asset with all six data answered.

    ``sign_intake_record`` refuses a receipt that cannot say which asset
    it is for, so every fixture that signs an intake record needs one of
    these rather than a half-filled row.
    """
    if client is None:
        client = Client.objects.create(name="Fixture Owner")
    fields = {
        "name": "Galeb",
        "asset_type": "Generator",
        "manufacturer": "Bavaria",
        "model": "S33",
        "registration_no": "TV-1234",
        "length_m": Decimal("9.50"),
    }
    fields.update(overrides)
    return Asset.objects.create(client=client, **fields)


def link_identified_asset(case, **overrides):
    """Attach a fully identified asset (and its owner) to ``case``."""
    asset = identified_asset(**overrides)
    case.client = asset.client
    case.asset = asset
    case.save(update_fields=["client", "asset"])
    return asset


def make_case(**raw):
    """Create a minimal SERVICE case with the given raw_* fields."""
    status, _ = Status.objects.get_or_create(
        case_type=CaseType.SERVICE,
        code="reported",
        defaults={"name": "Reported", "client_visible_name": "Reported"},
    )
    return Case.objects.create(
        case_type=CaseType.SERVICE,
        report_channel=ReportChannel.PORTAL,
        status=status,
        **raw,
    )


class ClientModelTests(TestCase):
    def test_create_client_with_asset(self):
        client = Client.objects.create(name="John Smith", phone="+44 67 123 456")
        asset = Asset.objects.create(
            client=client,
            name="Unit 7",
            asset_type="Generator",
            registration_no="TV-1234",
            length_m=Decimal("11.50"),
        )
        self.assertEqual(client.assets.count(), 1)
        self.assertEqual(str(client), "John Smith")
        self.assertIn("Unit 7", str(asset))
        self.assertFalse(client.is_company)


class NormalizePhoneTests(TestCase):
    def test_formatting_characters_stripped(self):
        self.assertEqual(normalize_phone("(067) 123-456"), "67123456")

    def test_prefix_equivalences(self):
        for variant in ("+44 67 123 456", "004467123456", "067-123-456", "67123456"):
            self.assertEqual(normalize_phone(variant), "67123456", variant)

    def test_empty_stays_empty(self):
        self.assertEqual(normalize_phone(""), "")

    def test_foreign_prefix_spellings_match_each_other(self):
        for country, variants in {
            "49": ("+49 91 123 456", "0049 91 123 456", "+49-91-123-456"),
            "33": ("+33 63 123 456", "0033 63 123 456"),
            "39": ("+39 333 123 4567", "0039 333 123 4567"),
        }.items():
            expected = normalize_phone(variants[0])
            self.assertTrue(expected.startswith(country), expected)
            for variant in variants[1:]:
                self.assertEqual(normalize_phone(variant), expected, variant)

    def test_a_foreign_number_does_not_collide_with_a_local_one(self):
        self.assertNotEqual(
            normalize_phone("+49 67 123 456"), normalize_phone("067 123 456")
        )


class NormalizeNameTests(TestCase):
    def test_case_and_whitespace_insensitive(self):
        for variant in ("Ana Petrović", "ana  petrović", "  ANA PETROVIĆ  "):
            self.assertEqual(normalize_name(variant), "ana petrović", variant)

    def test_empty_stays_empty(self):
        self.assertEqual(normalize_name(""), "")


class SuggestMatchesTests(TestCase):
    def setUp(self):
        self.by_phone = Client.objects.create(name="Phone Guy", phone="+44 67 123 456")
        self.by_email = Client.objects.create(name="Email Gal", email="Ana@Example.com")
        self.by_reg = Client.objects.create(name="Reg Owner")
        self.reg_asset = Asset.objects.create(
            client=self.by_reg,
            name="Unit 7",
            asset_type="Machine",
            registration_no="TV-1234",
            length_m=Decimal("10.00"),
        )

    def test_phone_match_with_normalization(self):
        case = make_case(raw_phone="067123456")
        # Reasons are translated; pin English so the assertion is stable.
        with translation.override("en"):
            matches = suggest_matches(case)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["client"], self.by_phone)
        self.assertIsNone(matches[0]["asset"])
        self.assertTrue(any("phone" in r for r in matches[0]["reasons"]))

    def test_email_match_case_insensitive(self):
        case = make_case(raw_email="ana@example.COM")
        matches = suggest_matches(case)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["client"], self.by_email)
        self.assertIsNone(matches[0]["asset"])

    def test_registration_match_inside_raw_asset_info(self):
        case = make_case(raw_asset_info="White machine, reg tv-1234, parked at bay 3")
        matches = suggest_matches(case)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["client"], self.by_reg)
        self.assertEqual(matches[0]["asset"], self.reg_asset)

    def test_blank_registration_numbers_never_match(self):
        Asset.objects.create(
            client=self.by_phone,
            name="No Reg",
            asset_type="Hand tool",
            registration_no="",
            length_m=Decimal("3.00"),
        )
        case = make_case(raw_asset_info="some asset description")
        self.assertEqual(suggest_matches(case), [])

    def test_ranking_registration_beats_phone_beats_email(self):
        case = make_case(
            raw_phone="+44 67 123 456",
            raw_email="ana@example.com",
            raw_asset_info="Reg TV-1234",
        )
        matches = suggest_matches(case)
        self.assertEqual(
            [m["client"] for m in matches],
            [self.by_reg, self.by_phone, self.by_email],
        )
        scores = [m["score"] for m in matches]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_multiple_channels_combine_for_one_client(self):
        combined = Client.objects.create(
            name="Combined", phone="068 555 555", email="combo@example.com"
        )
        asset = Asset.objects.create(
            client=combined,
            name="Combo Asset",
            asset_type="Generator",
            registration_no="KO-99",
            length_m=Decimal("8.00"),
        )
        case = make_case(
            raw_phone="+44 68 555-555",
            raw_email="COMBO@example.com",
            raw_asset_info="Registration KO-99",
        )
        matches = suggest_matches(case)
        top = matches[0]
        self.assertEqual(top["client"], combined)
        self.assertEqual(top["asset"], asset)
        self.assertEqual(len(top["reasons"]), 3)
        # Combined score strictly exceeds any single-channel score.
        self.assertTrue(all(top["score"] > m["score"] for m in matches[1:]))

    def test_name_match_normalized(self):
        case = make_case(raw_contact_name="  phone   GUY ")
        with translation.override("en"):
            matches = suggest_matches(case)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["client"], self.by_phone)
        self.assertTrue(any("name" in r for r in matches[0]["reasons"]))

    def test_name_and_phone_combine_for_one_client(self):
        case = make_case(raw_contact_name="Phone Guy", raw_phone="067123456")
        matches = suggest_matches(case)
        self.assertEqual(len(matches), 1)
        top = matches[0]
        self.assertEqual(top["client"], self.by_phone)
        self.assertEqual(len(top["reasons"]), 2)

    def test_no_writes(self):
        case = make_case(raw_phone="067123456")
        suggest_matches(case)
        case.refresh_from_db()
        self.assertIsNone(case.client)
        self.assertEqual(ActivityLog.objects.count(), 0)


class LinkCaseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staffer", password="x", is_staff=True
        )
        self.client_obj = Client.objects.create(name="Owner")
        self.asset = Asset.objects.create(
            client=self.client_obj,
            name="Asset",
            asset_type="Machine",
            length_m=Decimal("9.00"),
        )
        self.case = make_case(
            raw_contact_name="Owner Raw",
            raw_phone="067123456",
            raw_email="owner@example.com",
            raw_asset_info="an asset",
        )

    def test_links_client_and_asset(self):
        link_case(self.case, self.client_obj, asset=self.asset, user=self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.client, self.client_obj)
        self.assertEqual(self.case.asset, self.asset)

    def test_foreign_asset_rejected(self):
        other = Client.objects.create(name="Other")
        foreign = Asset.objects.create(
            client=other, name="Not Mine", asset_type="Jetski", length_m=Decimal("3.00")
        )
        with self.assertRaises(ValueError):
            link_case(self.case, self.client_obj, asset=foreign)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.client)
        self.assertEqual(ActivityLog.objects.count(), 0)

    def test_raw_fields_and_status_preserved(self):
        original_status_id = self.case.status_id
        link_case(self.case, self.client_obj, user=self.user)
        self.case.refresh_from_db()
        self.assertEqual(self.case.raw_contact_name, "Owner Raw")
        self.assertEqual(self.case.raw_phone, "067123456")
        self.assertEqual(self.case.raw_email, "owner@example.com")
        self.assertEqual(self.case.raw_asset_info, "an asset")
        self.assertEqual(self.case.status_id, original_status_id)

    def test_activity_log_written(self):
        link_case(self.case, self.client_obj, asset=self.asset, user=self.user)
        log = ActivityLog.objects.get(case=self.case)
        self.assertEqual(log.kind, ActivityLog.Kind.SYSTEM)
        self.assertEqual(log.author, self.user)
        self.assertIn("Owner", log.text)
        self.assertIn("Asset", log.text)


class TriageViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staffer", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.owner = Client.objects.create(name="Existing Owner", phone="067 111 222")
        self.asset = Asset.objects.create(
            client=self.owner,
            name="Blue Moon",
            asset_type="Machine",
            registration_no="TV-42",
            length_m=Decimal("12.00"),
        )
        self.case = make_case(
            raw_contact_name="Existing Owner",
            raw_phone="+44 67 111-222",
            raw_asset_info="Machine reg TV-42",
        )
        self.url = reverse("backoffice:case-triage", args=[self.case.pk])

    def test_anonymous_redirected_to_login(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_renders_raw_data_and_suggestions(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "+44 67 111-222")
        self.assertContains(response, "Existing Owner")
        self.assertContains(response, "Blue Moon")
        suggestions = response.context["suggestions"]
        self.assertEqual(suggestions[0]["client"], self.owner)
        self.assertEqual(suggestions[0]["asset"], self.asset)

    def test_post_link_suggestion(self):
        response = self.client.post(
            self.url,
            {"action": "link", "client_id": self.owner.pk, "asset_id": self.asset.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.client, self.owner)
        self.assertEqual(self.case.asset, self.asset)
        # Two entries now: the link itself, and the SVC-02 preliminary
        # work order that confirming a known client opens.
        log = ActivityLog.objects.filter(
            case=self.case, text__contains="linked to client"
        ).get()
        self.assertEqual(log.kind, ActivityLog.Kind.SYSTEM)
        self.assertEqual(log.author, self.user)
        self.assertTrue(
            self.case.work_order.is_preliminary,
            "SVC-02: confirming a known client opens the preliminary order.",
        )

    def test_post_create_new_client_with_asset(self):
        response = self.client.post(
            self.url,
            {
                "action": "create",
                "name": "New Owner",
                "phone": "068 999 999",
                "email": "new@example.com",
                "asset_name": "Fresh Asset",
                "asset_registration": "BD-77",
                "asset_length_m": "7.50",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.client.name, "New Owner")
        self.assertEqual(self.case.asset.name, "Fresh Asset")
        self.assertEqual(self.case.asset.length_m, Decimal("7.50"))
        self.assertEqual(self.case.raw_contact_name, "Existing Owner")
        self.assertTrue(ActivityLog.objects.filter(case=self.case).exists())

    def test_post_create_asset_without_length_rejected(self):
        response = self.client.post(
            self.url,
            {
                "action": "create",
                "name": "New Owner",
                "asset_name": "No Length",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "asset_length_m",
            "Asset length is required to create an asset.",
        )
        self.case.refresh_from_db()
        self.assertIsNone(self.case.client)


class TriageAssetPickTests(TestCase):
    """Triage can attach an asset the client already has on file.

    Suggestions carry an asset only when a registration number appears in
    the raw text. A staff-entered case rarely has one, so the Link row
    used to attach the client alone - and the asset got created a second
    time by whichever intake form came next.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staffer", password="x", is_staff=True
        )
        self.client.force_login(self.user)
        self.owner = Client.objects.create(name="Existing Owner", phone="067 111 222")
        self.asset = Asset.objects.create(
            client=self.owner,
            name="Blue Moon",
            asset_type="Machine",
            registration_no="TV-42",
            length_m=Decimal("12.00"),
        )
        other = Client.objects.create(name="Somebody Else")
        self.other_asset = Asset.objects.create(
            client=other, name="Red Sun", asset_type="Motor", length_m=Decimal("8.00")
        )
        # Matched by phone only - no registration anywhere in the raw text.
        self.case = make_case(
            raw_contact_name="Existing Owner",
            raw_phone="+44 67 111-222",
            raw_asset_info="the blue machine",
        )
        self.url = reverse("backoffice:case-triage", args=[self.case.pk])

    def test_client_suggestion_offers_only_that_clients_assets(self):
        response = self.client.get(self.url)
        self.assertIsNone(response.context["suggestions"][0]["asset"])
        self.assertContains(response, 'name="asset_id"')
        self.assertContains(response, "Blue Moon")
        self.assertNotContains(response, "Red Sun")

    def test_link_with_a_picked_asset_attaches_it(self):
        response = self.client.post(
            self.url,
            {"action": "link", "client_id": self.owner.pk, "asset_id": self.asset.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset, self.asset)

    def test_link_without_a_pick_still_attaches_the_client_alone(self):
        self.client.post(
            self.url, {"action": "link", "client_id": self.owner.pk, "asset_id": ""}
        )
        self.case.refresh_from_db()
        self.assertEqual(self.case.client, self.owner)
        self.assertIsNone(self.case.asset)

    def test_somebody_elses_asset_is_a_404_not_a_link(self):
        response = self.client.post(
            self.url,
            {
                "action": "link",
                "client_id": self.owner.pk,
                "asset_id": self.other_asset.pk,
            },
        )
        self.assertEqual(response.status_code, 404)
        self.case.refresh_from_db()
        self.assertIsNone(self.case.client)
