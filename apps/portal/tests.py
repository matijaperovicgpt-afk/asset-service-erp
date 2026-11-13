"""Tests for the public client portal."""

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.attachments.models import Attachment
from apps.cases.models import Case, CaseType, Status
from apps.portal.forms import MAX_PHOTO_SIZE, LOCATION_CHOICES, PublicReportForm

#: Smallest valid GIF payload - enough for upload tests.
TINY_GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


class PortalSmokeTests(TestCase):
    def setUp(self):
        cache.clear()  # POR-04: 5 posts/h per IP would block the 6th test here
        self.new_report = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Report received",
            sort_order=1,
            is_terminal=False,
        )
        self.valid_data = {
            "contact_name": "John Smith",
            "phone": "+44 67 000 000",
            "email": "mark@example.com",
            "asset_name": "Beneteau Oceanis 38",
            "site_position": "C12",
            "location": "Main workshop",
            "symptoms_description": "Engine overheats after 10 minutes.",
            "website": "",  # honeypot left empty
        }

    def test_valid_submission_creates_case_with_public_token(self):
        response = self.client.post(reverse("portal:report"), self.valid_data)
        self.assertEqual(Case.objects.count(), 1)
        case = Case.objects.get()
        self.assertEqual(case.case_type, CaseType.SERVICE)
        self.assertEqual(case.report_channel, "PORTAL")
        self.assertEqual(case.status, self.new_report)
        self.assertTrue(case.public_token)
        self.assertEqual(
            Case.objects.filter(public_token=case.public_token).count(), 1
        )
        self.assertRedirects(response, reverse("portal:thanks"))

    def test_thanks_page_names_nothing_about_the_case(self):
        """No tracking page exists, so the thank-you page hands out no
        link and no token: the client hears from staff next."""
        self.client.post(reverse("portal:report"), self.valid_data)
        case = Case.objects.get()
        response = self.client.get(reverse("portal:thanks"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Thank you")
        self.assertNotContains(response, case.public_token)
        self.assertNotContains(response, "/track/")
        self.assertNotContains(response, self.new_report.client_visible_name)

    def test_tracking_page_no_longer_exists(self):
        self.client.post(reverse("portal:report"), self.valid_data)
        case = Case.objects.get()
        for url in (f"/track/{case.public_token}/", f"/track/{case.public_token}/quote/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_honeypot_filled_post_does_not_create_case(self):
        data = dict(self.valid_data, website="http://spam.example.com")
        response = self.client.post(reverse("portal:report"), data)
        # Fake success: bot sees a normal page, nothing is stored.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Case.objects.count(), 0)

    # NTF-01: staff are notified about every new portal submission.
    @override_settings(STAFF_NOTIFICATION_EMAILS=["desk@example.com"])
    def test_submission_notifies_staff_after_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("portal:report"), self.valid_data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["desk@example.com"])
        self.assertIn("John Smith", mail.outbox[0].body)

    # POR-04: uploaded files are stored under randomized names.
    def test_uploaded_photo_gets_randomized_stored_name(self):
        data = dict(
            self.valid_data,
            photos=SimpleUploadedFile(
                "my holiday photo.gif", TINY_GIF, content_type="image/gif"
            ),
        )
        self.client.post(reverse("portal:report"), data)
        attachment = Attachment.objects.get()
        stored_name = attachment.file.name.rsplit("/", 1)[-1]
        self.assertNotIn("my holiday photo", stored_name)
        self.assertRegex(stored_name, r"^[0-9a-f]{32}\.gif$")
        self.assertEqual(attachment.original_name, "my holiday photo.gif")
        attachment.file.delete(save=False)


class PortalRateLimitTests(TestCase):
    """POR-04: the public report form is rate limited per IP."""

    def setUp(self):
        cache.clear()  # rate-limit counters live in the cache
        Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Report received",
        )
        self.valid_data = {
            "contact_name": "John Smith",
            "phone": "+44 67 000 000",
            "asset_name": "Beneteau Oceanis 38",
            "symptoms_description": "Engine overheats.",
            "location": "Customer site",
            "website": "",
        }

    def tearDown(self):
        cache.clear()

    def test_sixth_submission_within_an_hour_is_blocked(self):
        for _ in range(5):
            response = self.client.post(reverse("portal:report"), self.valid_data)
            self.assertEqual(response.status_code, 302)
        response = self.client.post(reverse("portal:report"), self.valid_data)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Case.objects.count(), 5)

    def test_get_requests_are_not_rate_limited(self):
        for _ in range(10):
            response = self.client.get(reverse("portal:report"))
            self.assertEqual(response.status_code, 200)


class StructuredAssetInfoTests(TestCase):
    """GEN-01/GEN-03: structured asset fields compose into the
    raw_asset_info audit string with fixed label prefixes."""

    def setUp(self):
        cache.clear()  # POR-04 rate-limit counters leak across classes
        Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Report received",
        )
        self.data = {
            "contact_name": "John Smith",
            "phone": "+44 67 000 000",
            "asset_name": "Unit 7",
            "asset_model": "Beneteau Oceanis 38",
            "asset_length_m": "11.50",
            "site_position": "C12",
            "asset_notes": "Registracija TV-0042, dizel motor.",
            "symptoms_description": "Engine overheats.",
            "location": "Customer site",
            "website": "",
        }

    def test_full_composition(self):
        self.client.post(reverse("portal:report"), self.data)
        case = Case.objects.get()
        self.assertEqual(
            case.raw_asset_info,
            "Asset: Unit 7\n"
            "Model: Beneteau Oceanis 38\n"
            "Length (m): 11.50\n"
            "Position: C12\n"
            "Notes: Registracija TV-0042, dizel motor.",
        )

    def test_minimal_composition_only_required_fields(self):
        data = {
            "contact_name": "Mark",
            "phone": "067123456",
            "asset_name": "Unit 7",
            "symptoms_description": "x",
            "location": "Customer site",
            "website": "",
        }
        self.client.post(reverse("portal:report"), data)
        case = Case.objects.get()
        self.assertEqual(case.raw_asset_info, "Asset: Unit 7")

    def test_triage_matches_registration_typed_in_notes(self):
        from apps.clients.models import Client, Asset
        from apps.clients.services import suggest_matches

        client = Client.objects.create(name="John Smith")
        asset = Asset.objects.create(
            client=client,
            name="Unit 7",
            asset_type="Machine",
            registration_no="TV-0042",
            length_m="11.50",
        )
        self.client.post(reverse("portal:report"), self.data)
        case = Case.objects.get()
        matches = suggest_matches(case)
        self.assertTrue(matches)
        self.assertEqual(matches[0]["asset"], asset)


class LocationSelectorTests(TestCase):
    """The location is a port choice list + free-text Other."""

    def setUp(self):
        cache.clear()  # POR-04 rate-limit counters leak across classes
        Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Report received",
        )
        self.base = {
            "contact_name": "Mark",
            "phone": "067123456",
            "asset_name": "Unit 7",
            "symptoms_description": "x",
            # §7.3: one click from the curated list, required since the
            # phase-5 audit - a report that does not say where the asset
            # is cannot be acted on.
            "location": "Customer site",
            "website": "",
        }

    def test_selected_port_stored_in_location(self):
        data = dict(self.base, location="Main workshop")
        self.client.post(reverse("portal:report"), data)
        self.assertEqual(Case.objects.get().location, "Main workshop")

    def test_other_requires_free_text(self):
        data = dict(self.base, location="__other__", location_other="")
        response = self.client.post(reverse("portal:report"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Case.objects.count(), 0)
        self.assertContains(response, "Please describe the location.")

    def test_other_free_text_stored_in_location(self):
        data = dict(
            self.base, location="__other__", location_other="Private yard, Riverside"
        )
        self.client.post(reverse("portal:report"), data)
        self.assertEqual(Case.objects.get().location, "Private yard, Riverside")

    def test_unknown_port_value_is_rejected(self):
        data = dict(self.base, location="Site Fake")
        response = self.client.post(reverse("portal:report"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Case.objects.count(), 0)


class VideoUploadTests(TestCase):
    """POR-01: the portal accepts videos (mp4/mov) next to photos."""

    def setUp(self):
        cache.clear()
        Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Report received",
        )
        self.base = {
            "contact_name": "Mark",
            "phone": "067123456",
            "asset_name": "Unit 7",
            "symptoms_description": "x",
            # §7.3: one click from the curated list, required since the
            # phase-5 audit - a report that does not say where the asset
            # is cannot be acted on.
            "location": "Customer site",
            "website": "",
        }

    def test_mp4_creates_video_attachment(self):
        data = dict(
            self.base,
            photos=SimpleUploadedFile(
                "engine noise.mp4", b"fake-mp4-bytes", content_type="video/mp4"
            ),
        )
        response = self.client.post(reverse("portal:report"), data)
        self.assertEqual(response.status_code, 302)
        attachment = Attachment.objects.get()
        self.assertEqual(attachment.kind, Attachment.Kind.VIDEO)
        self.assertEqual(attachment.original_name, "engine noise.mp4")
        attachment.file.delete(save=False)

    # Size caps are asserted on the form directly: the test client
    # re-serializes uploads, so a faked .size would not survive HTTP.
    def _form_with_upload(self, upload):
        from apps.portal.forms import PublicReportForm

        return PublicReportForm(data=self.base, files={"photos": upload})

    def test_oversized_video_is_rejected(self):
        big = SimpleUploadedFile("clip.mp4", b"x", content_type="video/mp4")
        big.size = 50 * 1024 * 1024 + 1
        form = self._form_with_upload(big)
        self.assertFalse(form.is_valid())
        self.assertIn("Maximum size is 50 MB", str(form.errors["photos"]))

    def test_oversized_photo_still_rejected_at_10mb(self):
        big = SimpleUploadedFile("big.jpg", b"x", content_type="image/jpeg")
        big.size = 10 * 1024 * 1024 + 1
        form = self._form_with_upload(big)
        self.assertFalse(form.is_valid())
        self.assertIn("Maximum size is 10 MB", str(form.errors["photos"]))

    def test_mixed_file_cap_is_five(self):
        files = [
            SimpleUploadedFile("p%d.gif" % i, TINY_GIF, content_type="image/gif")
            for i in range(4)
        ] + [
            SimpleUploadedFile("v%d.mp4" % i, b"fake", content_type="video/mp4")
            for i in range(2)
        ]
        data = dict(self.base, photos=files)
        response = self.client.post(reverse("portal:report"), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Case.objects.count(), 0)
        self.assertContains(response, "at most 5 files")

    def test_other_field_progressive_disclosure_hooks_present(self):
        response = self.client.get(reverse("portal:report"))
        self.assertContains(response, 'id="field-location_other"')
        self.assertContains(response, 'select.value === "__other__"')


class ReportFormDownscaleMarkupTests(TestCase):
    """POR-05: the browser reads its limits from the rendered markup."""

    def test_upload_zone_carries_the_downscale_configuration(self):
        response = self.client.get(reverse("portal:report"))
        self.assertContains(response, 'data-max-files="5"')
        self.assertContains(response, 'data-max-edge="2000"')
        self.assertContains(response, 'data-min-bytes="1048576"')

    def test_quality_renders_with_a_decimal_point_under_sr_latn(self):
        """The L10N trap: "0,82" would make parseFloat() read 0.

        A silently disabled feature with no error anywhere - the same
        class of bug as the localized numbers in the storage SVG.

        The locale MUST be forced with the cookie, not with
        ``translation.override``: CookieLocaleMiddleware re-activates the
        language on every request, so an override wrapped around
        ``self.client.get()`` is discarded and this test would quietly
        run in English and prove nothing.
        """
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sr-latn"
        response = self.client.get(reverse("portal:report"))
        # Proof the page really is Serbian, so the assertions below mean
        # something (and that django.mo carries the new string).
        self.assertContains(response, "Priprema fotografija...")
        self.assertContains(response, 'data-quality="0.82"')
        self.assertNotContains(response, 'data-quality="0,82"')

    def test_server_side_validators_are_unchanged(self):
        """POR-04 stays the authority; the browser work is advisory."""
        oversized = SimpleUploadedFile(
            "huge.jpg", b"x" * (MAX_PHOTO_SIZE + 1), content_type="image/jpeg"
        )
        form = PublicReportForm(
            data={
                "contact_name": "Ana",
                "phone": "+4469000000",
                "asset_name": "Test",
                "asset_length_m": "8",
                "location": LOCATION_CHOICES[0][0],
                "symptoms_description": "Engine will not start.",
            },
            files={"photos": [oversized]},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("photos", form.errors)
