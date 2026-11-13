"""Tests for the WNT-02 "Unos u bazu" storage database-entry view."""

import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.attachments.models import Attachment
from apps.cases.models import ActivityLog, Case, CaseType, ReportChannel
from apps.cases.tests import build_workflow
from apps.clients.models import Client, Asset
from apps.portal.forms import OTHER_LOCATION
from apps.storage.models import StorageAgreement, StorageServiceType

WINTER_CODES = [
    "inquiry",
    "quote_sent",
    "contracted",
    "asset_received",
    "in_storage",
    "ready_for_pickup",
    "closed",
]


class StorageIntakeViewTests(TestCase):
    """WNT-02: one form records client, asset and agreement data."""

    def setUp(self):
        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="John Smith",
            raw_phone="+44 67 111 222",
        )
        self.service_type = StorageServiceType.objects.create(
            name="Storage — standard"
        )
        self.staff = get_user_model().objects.create_user(
            username="storage_staff", password="demo12345", is_staff=True
        )
        self.client.force_login(self.staff)
        self.url = reverse("backoffice:storage-intake", args=[self.case.pk])

    def _post_data(self, **overrides):
        data = {
            "existing_client": "",
            "name": "John Smith",
            "phone": "+44 67 111 222",
            "email": "",
            "asset_name": "Unit Alpha",
            "asset_type": "Compressor",
            "asset_registration": "TV-1234",
            "asset_length_m": "11.50",
            "ownership": "John Smith (sole owner)",
            "origin_site": "Customer site",
            "origin_site_other": "",
            "selected_services": [str(self.service_type.pk)],
            "package": "",
        }
        data.update(overrides)
        return data

    # --- access control -------------------------------------------------

    def test_anonymous_request_redirects_to_login(self):
        from django.conf import settings

        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response.url)

    def test_non_staff_user_gets_403(self):
        outsider = get_user_model().objects.create_user(
            username="outsider", password="demo12345", is_staff=False
        )
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_non_storage_case_404s(self):
        service_status = build_workflow(CaseType.SERVICE, ["new_report"])
        service_case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=service_status["new_report"],
            report_channel=ReportChannel.PHONE,
        )
        url = reverse("backoffice:storage-intake", args=[service_case.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_get_renders_form(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unos u bazu")

    def test_correcting_the_length_updates_the_linked_asset(self):
        """WNT-02 prefills this field from the asset and presents it as the
        asset's length, but it only ever reached the branch that creates a
        new Asset. Correcting it on an already-linked case wrote the new
        number onto the agreement alone, leaving the asset card beside it,
        the asset page and the transport recommendation on the old one -
        two lengths for one asset, with nothing saying which was in use.
        """
        self.client.post(self.url, self._post_data())
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset.length_m, Decimal("11.50"))

        self.client.post(self.url, self._post_data(asset_length_m="12.80"))
        self.case.refresh_from_db()
        self.case.asset.refresh_from_db()
        self.assertEqual(self.case.asset.length_m, Decimal("12.80"))
        self.assertEqual(
            self.case.storage_agreement.asset_length_m, Decimal("12.80")
        )

    # --- POST: create paths ---------------------------------------------

    def test_post_creates_client_asset_and_agreement(self):
        response = self.client.post(self.url, self._post_data())
        self.assertRedirects(
            response, reverse("backoffice:case-detail", args=[self.case.pk])
        )

        client = Client.objects.get(name="John Smith")
        asset = Asset.objects.get(name="Unit Alpha")
        self.assertEqual(asset.client, client)
        self.assertEqual(asset.length_m, Decimal("11.50"))

        self.case.refresh_from_db()
        self.assertEqual(self.case.client, client)
        self.assertEqual(self.case.asset, asset)
        # Audit fields and workflow status stay untouched (GEN-03 / INV-01).
        self.assertEqual(self.case.raw_contact_name, "John Smith")
        self.assertEqual(self.case.status, self.statuses["inquiry"])

        agreement = self.case.storage_agreement
        self.assertEqual(agreement.asset_length_m, Decimal("11.50"))
        self.assertEqual(agreement.ownership, "John Smith (sole owner)")
        self.assertEqual(agreement.origin_site, "Customer site")
        self.assertEqual(
            list(agreement.selected_services.all()), [self.service_type]
        )

        log_texts = list(
            ActivityLog.objects.filter(case=self.case).values_list(
                "text", flat=True
            )
        )
        self.assertTrue(any("WNT-02" in text for text in log_texts))

    def test_other_site_free_text_is_resolved(self):
        response = self.client.post(
            self.url,
            self._post_data(
                origin_site=OTHER_LOCATION, origin_site_other="West Site"
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.storage_agreement.origin_site, "West Site")

    def test_post_with_existing_client_creates_no_new_client(self):
        existing = Client.objects.create(name="Existing Client")
        response = self.client.post(
            self.url,
            self._post_data(existing_client=str(existing.pk), name=""),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Client.objects.count(), 1)
        asset = Asset.objects.get(name="Unit Alpha")
        self.assertEqual(asset.client, existing)
        self.case.refresh_from_db()
        self.assertEqual(self.case.client, existing)

    def test_post_on_already_linked_case_only_records_agreement(self):
        client = Client.objects.create(name="Linked Client")
        asset = Asset.objects.create(
            client=client,
            name="Vezana Barka",
            asset_type="",
            length_m=Decimal("9.00"),
        )
        self.case.client = client
        self.case.asset = asset
        self.case.save(update_fields=["client", "asset"])

        response = self.client.post(
            self.url,
            {
                "asset_length_m": "9.00",
                "ownership": "Linked Client",
                "origin_site": "Main workshop",
                "origin_site_other": "",
                "selected_services": [str(self.service_type.pk)],
                "package": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Client.objects.count(), 1)
        self.assertEqual(Asset.objects.count(), 1)
        self.case.refresh_from_db()
        self.assertEqual(self.case.storage_agreement.ownership, "Linked Client")
        # No duplicate "linked during triage" entry - links were unchanged.
        self.assertFalse(
            ActivityLog.objects.filter(
                case=self.case, text__contains="during triage"
            ).exists()
        )

    def test_post_completes_existing_agreement_without_duplicating_it(self):
        # WNT-01 inquiry documentation may have created the agreement first.
        StorageAgreement.objects.create(
            case=self.case, permit_no="NP-555", asset_length_m=None
        )
        response = self.client.post(self.url, self._post_data())
        self.assertEqual(response.status_code, 302)
        agreement = StorageAgreement.objects.get(case=self.case)
        self.assertEqual(StorageAgreement.objects.count(), 1)
        self.assertEqual(agreement.permit_no, "NP-555")
        self.assertEqual(agreement.ownership, "John Smith (sole owner)")
        self.assertEqual(agreement.asset_length_m, Decimal("11.50"))

    # --- POST: ownership documents ---------------------------------------

    def test_ownership_documents_attach_to_case_with_derived_kinds(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            data = self._post_data()
            data["ownership_documents"] = [
                SimpleUploadedFile("vlasnistvo.jpg", b"fake-jpeg"),
                SimpleUploadedFile("ugovor.pdf", b"%PDF-1.4 contract"),
            ]
            response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)

        attachments = Attachment.objects.all()
        self.assertEqual(attachments.count(), 2)
        kinds = {a.original_name: a.kind for a in attachments}
        self.assertEqual(kinds["vlasnistvo.jpg"], Attachment.Kind.PHOTO)
        self.assertEqual(kinds["ugovor.pdf"], Attachment.Kind.DOCUMENT)
        self.assertTrue(
            all(a.stage == Attachment.Stage.REPORT for a in attachments)
        )
        self.assertTrue(
            ActivityLog.objects.filter(
                case=self.case, text__contains="ownership document"
            ).exists()
        )

    def test_rejected_file_rolls_back_everything(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, True)
        with override_settings(MEDIA_ROOT=media_root):
            data = self._post_data()
            data["ownership_documents"] = [
                SimpleUploadedFile("virus.exe", b"MZ not-a-document")
            ]
            response = self.client.post(self.url, data)
        # Form re-rendered with the attachment error; the atomic block
        # rolled back the client, asset and agreement.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Client.objects.count(), 0)
        self.assertEqual(Asset.objects.count(), 0)
        self.assertFalse(StorageAgreement.objects.exists())
        self.assertFalse(Attachment.objects.exists())

    # --- POST: validation -------------------------------------------------

    def test_missing_ownership_is_a_field_error(self):
        response = self.client.post(self.url, self._post_data(ownership=""))
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"], "ownership", "This field is required."
        )
        self.assertFalse(StorageAgreement.objects.exists())
        self.assertEqual(Client.objects.count(), 0)

    def test_missing_asset_name_is_a_field_error(self):
        response = self.client.post(self.url, self._post_data(asset_name=""))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StorageAgreement.objects.exists())

    def test_both_existing_and_new_client_is_an_error(self):
        existing = Client.objects.create(name="Existing Client")
        response = self.client.post(
            self.url, self._post_data(existing_client=str(existing.pk))
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StorageAgreement.objects.exists())


class StorageIntakeExistingAssetTests(TestCase):
    """WNT-02 can link an asset already on file instead of creating a twin."""

    def setUp(self):
        self.statuses = build_workflow(CaseType.STORAGE, WINTER_CODES)
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=self.statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="John Smith",
        )
        self.service_type = StorageServiceType.objects.create(name="Storage")
        self.owner = Client.objects.create(name="John Smith", phone="+44 67 111 222")
        self.asset = Asset.objects.create(
            client=self.owner,
            name="Unit Alpha",
            asset_type="Compressor",
            registration_no="TV-1234",
            length_m=Decimal("11.50"),
        )
        other = Client.objects.create(name="Someone Else")
        self.other_asset = Asset.objects.create(
            client=other, name="Unit Bravo", asset_type="Motor", length_m=Decimal("7.00")
        )
        staff = get_user_model().objects.create_user(
            username="storage_staff", password="demo12345", is_staff=True
        )
        self.client.force_login(staff)
        self.url = reverse("backoffice:storage-intake", args=[self.case.pk])

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
            "asset_length_m": "11.50",
            "ownership": "John Smith",
            "origin_site": "Customer site",
            "origin_site_other": "",
            "selected_services": [str(self.service_type.pk)],
            "package": "",
        }
        data.update(overrides)
        return data

    def _link_client(self):
        self.case.client = self.owner
        self.case.save(update_fields=["client"])

    def test_linked_client_is_offered_only_its_own_assets(self):
        self._link_client()
        response = self.client.get(self.url)
        self.assertContains(response, "Existing asset")
        self.assertContains(response, "Unit Alpha")
        self.assertNotContains(response, "Unit Bravo")

    def test_picking_the_asset_links_it_and_creates_nothing(self):
        self._link_client()
        response = self.client.post(
            self.url, self._post_data(existing_asset=str(self.asset.pk))
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.asset, self.asset)
        self.assertEqual(Asset.objects.count(), 2)

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
        self.assertEqual(Client.objects.count(), 2)

    def test_asset_alone_adopts_its_owner(self):
        response = self.client.post(
            self.url, self._post_data(existing_asset=str(self.asset.pk))
        )
        self.assertEqual(response.status_code, 302)
        self.case.refresh_from_db()
        self.assertEqual(self.case.client, self.owner)
        self.assertEqual(self.case.asset, self.asset)

    def test_asset_of_another_client_is_refused(self):
        response = self.client.post(
            self.url,
            self._post_data(
                existing_client=str(self.owner.pk),
                existing_asset=str(self.other_asset.pk),
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "belongs to a different client")
        self.case.refresh_from_db()
        self.assertIsNone(self.case.client)

    def test_asset_plus_new_client_name_is_refused(self):
        response = self.client.post(
            self.url,
            self._post_data(name="New Client", existing_asset=str(self.asset.pk)),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already has an owner")
        self.assertEqual(Client.objects.count(), 2)

    def test_creating_a_asset_still_needs_its_name(self):
        """The pick relaxes the name requirement; leaving both empty does not."""
        self._link_client()
        response = self.client.post(self.url, self._post_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Asset name is required.")

    def test_no_assets_on_file_hides_the_picker(self):
        Asset.objects.all().delete()
        response = self.client.get(self.url)
        self.assertNotContains(response, "Existing asset")
