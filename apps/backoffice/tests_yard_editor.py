"""WNT-09: the yard editor and the plan that reads from it.

* who may change the yard (the managerial pair, like the price lists);
* a save that leaves a parked asset outside the rectangle says so;
* the site plan is stored and drawn under the plan;
* the plan draws outlines over footprints, locale-proof.
"""

import io
import json
import re
import shutil
import tempfile
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.backoffice.tests_storage_svg_l10n import SvgNumberAssertionsMixin
from apps.cases.models import Case, CaseType, ReportChannel, Status
from apps.storage.models import StorageAgreement, StorageObstacle, StorageZone
from apps.storage.services import place_asset

_EDITOR_DATA_RE = re.compile(
    r'<script id="yard-editor-data" type="application/json">(.*?)</script>', re.S
)


def _png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 3), (200, 210, 220)).save(buf, format="PNG")
    return buf.getvalue()


class YardEditorTestBase(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp(prefix="yard-media-")
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        self.zone = StorageZone.objects.create(
            name="Otvoreno dvoriste",
            width_m=Decimal("40"),
            length_m=Decimal("25"),
        )
        User = get_user_model()
        self.manager = User.objects.create_user(
            username="mgr", password="demo12345", is_staff=True, role="MANAGER"
        )
        self.clerk = User.objects.create_user(
            username="desk", password="demo12345", is_staff=True, role="FRONT_DESK"
        )
        self.client.force_login(self.manager)

    def edit_url(self, zone=None):
        zone = zone or self.zone
        return reverse("backoffice:storage-zone-edit", args=[zone.pk])

    def payload(self, **over):
        data = {
            "name": "Otvoreno dvoriste",
            "width_m": "40",
            "length_m": "25",
            "clearance_m": "0.75",
            "sort_order": "0",
            "is_active": "on",
        }
        data.update(over)
        return data

    def make_placed_case(self, x, y, length="10", width="4"):
        status, _ = Status.objects.get_or_create(
            case_type=CaseType.STORAGE,
            code="in_storage",
            defaults={"name": "In storage", "client_visible_name": "In storage"},
        )
        case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=status,
        )
        agreement = StorageAgreement.objects.create(
            case=case,
            asset_length_m=Decimal(length),
            asset_width_m=Decimal(width),
        )
        place_asset(agreement, self.zone, Decimal(x), Decimal(y))
        return case


class AccessTests(YardEditorTestBase):
    def test_front_desk_is_sent_back_to_the_plan(self):
        self.client.force_login(self.clerk)
        response = self.client.get(self.edit_url())
        self.assertRedirects(response, reverse("backoffice:storage-layout"))
        response = self.client.post(self.edit_url(), self.payload(name="Hacked"))
        self.assertEqual(response.status_code, 302)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.name, "Otvoreno dvoriste")

    def test_manager_gets_the_editor(self):
        response = self.client.get(self.edit_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="yard-editor-svg"')
        self.assertContains(response, "yard-editor.js")
        # A plain form, not a boosted one: an upload and a visible error.
        self.assertContains(response, 'hx-boost="false"')

    def test_plan_links_to_the_editor_only_for_editors(self):
        response = self.client.get(reverse("backoffice:storage-layout"))
        self.assertContains(response, self.edit_url())
        self.client.force_login(self.clerk)
        response = self.client.get(reverse("backoffice:storage-layout"))
        self.assertNotContains(response, self.edit_url())


class SaveTests(YardEditorTestBase):
    def test_save_changes_the_rectangle(self):
        response = self.client.post(self.edit_url(), self.payload(width_m="55"))
        self.assertRedirects(response, self.edit_url())
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.width_m, Decimal("55"))

    def test_create_a_new_yard(self):
        response = self.client.post(
            reverse("backoffice:storage-zone-add"), self.payload(name="Hala 2")
        )
        zone = StorageZone.objects.get(name="Hala 2")
        self.assertRedirects(response, self.edit_url(zone))

    def test_bad_numbers_are_refused_in_place(self):
        response = self.client.post(self.edit_url(), self.payload(width_m="-3"))
        self.assertEqual(response.status_code, 200)
        self.zone.refresh_from_db()
        self.assertEqual(self.zone.width_m, Decimal("40"))

    def test_shrinking_reports_the_assets_it_strands(self):
        stranded = self.make_placed_case("30", "2")
        safe = self.make_placed_case("2", "2")
        response = self.client.post(
            self.edit_url(), self.payload(width_m="20"), follow=True
        )
        text = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("#%s" % stranded.pk, text)
        self.assertNotIn("#%s" % safe.pk, text)

    def test_obstacles_survive_a_save(self):
        StorageObstacle.objects.create(
            zone=self.zone, name="Lane", pos_x_m=0, pos_y_m=12, width_m=40, length_m=3
        )
        self.client.post(self.edit_url(), self.payload())
        self.assertEqual(self.zone.obstacles.count(), 1)

    def test_plan_image_is_stored_and_drawn(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            upload = SimpleUploadedFile("plan.png", _png_bytes(), content_type="image/png")
            response = self.client.post(self.edit_url(), self.payload(plan_image=upload))
            self.assertEqual(response.status_code, 302)
            self.zone.refresh_from_db()
            self.assertTrue(self.zone.plan_image.name.startswith("yard_plans/"))
            response = self.client.get(reverse("backoffice:storage-layout"))
            self.assertContains(response, 'class="yard-plan-image"')
            self.assertContains(response, self.zone.plan_image.url)
            response = self.client.get(reverse("backoffice:storage-fit-check"))
            self.assertContains(response, self.zone.plan_image.url)
            response = self.client.get(self.edit_url())
            self.assertContains(response, self.zone.plan_image.url)

    def test_non_image_upload_is_refused(self):
        with override_settings(MEDIA_ROOT=self.media_root):
            upload = SimpleUploadedFile("plan.txt", b"not a picture", content_type="text/plain")
            response = self.client.post(self.edit_url(), self.payload(plan_image=upload))
            self.assertEqual(response.status_code, 200)
            self.zone.refresh_from_db()
            self.assertFalse(self.zone.plan_image)

    def test_preview_carries_assets_and_obstacles(self):
        case = self.make_placed_case("2", "2")
        StorageObstacle.objects.create(
            zone=self.zone, name="Lane", pos_x_m=0, pos_y_m=12, width_m=40, length_m=3
        )
        response = self.client.get(self.edit_url())
        data = json.loads(_EDITOR_DATA_RE.search(response.content.decode()).group(1))
        self.assertEqual([b["case"] for b in data["assets"]], [case.pk])
        self.assertTrue(data["assets"][0]["d"].startswith("M "))
        self.assertEqual([o["name"] for o in data["obstacles"]], ["Lane"])


class PlanRenderingTests(SvgNumberAssertionsMixin, YardEditorTestBase):
    def setUp(self):
        super().setUp()
        self.case = self.make_placed_case("22", "12")

    def test_plan_draws_a_outline_over_the_footprint(self):
        response = self.client.get(reverse("backoffice:storage-layout"))
        svg = self.svg_blocks(response.content.decode())[0]
        self.assertRectDrawn(svg, 22, 12, 4, 10)
        self.assertIn('class="yard-outline"', svg)
        self.assertIn('d="M 24.00 12.00 ', svg)

    def test_fit_check_uses_the_same_drawing(self):
        response = self.client.get(reverse("backoffice:storage-fit-check"))
        svg = self.svg_blocks(response.content.decode())[0]
        self.assertIn('class="yard-outline"', svg)
        self.assertIn("yard-picking", svg)

    def test_placer_payload_carries_the_outline(self):
        response = self.client.get(
            reverse("backoffice:storage-layout") + "?case=%s" % self.case.pk
        )
        data = json.loads(
            re.search(
                r'<script id="yard-data" type="application/json">(.*?)</script>',
                response.content.decode(),
                re.S,
            ).group(1)
        )
        self.assertTrue(data[0]["assets"][0]["d"].startswith("M "))

    def test_serbian_locale_leaves_the_geometry_alone(self):
        self.client.cookies["django_language"] = "sr-latn"
        response = self.client.get(reverse("backoffice:storage-layout"))
        html = response.content.decode()
        svg = self.svg_blocks(html)[0]
        for attr in re.findall(r'\bd="([^"]*)"', svg):
            self.assertNotIn(",", attr, attr)
        self.assertViewBoxIsWellFormed(svg)
        self.assertRectDrawn(svg, 22, 12, 4, 10)
