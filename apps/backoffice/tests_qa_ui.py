"""QA review 2026-09-28, UI findings M4 / M6 / L3 / L6 / L7."""

import shutil
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.views.defaults import server_error

from apps.cases.models import Case, CaseType, ReportChannel
from apps.cases.tests import build_service_workflow, build_workflow
from apps.clients.models import Client
from apps.parts.models import Part


def _staff(username="qa_staff", **extra):
    return get_user_model().objects.create_user(
        username=username, password="test-pass-123", is_staff=True, **extra
    )


class NoStoreMiddlewareTests(TestCase):
    """UI-M6: Back after sign-out must not redisplay a cached client page."""

    @classmethod
    def setUpClass(cls):
        cls.media_root = tempfile.mkdtemp(prefix="test-media-qa-")
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_root)
        cls.media_override.enable()
        cls.addClassCleanup(cls.media_override.disable)
        super().setUpClass()

    @classmethod
    def setUpTestData(cls):
        cls.user = _staff()
        photo = Path(cls.media_root) / "cases" / "1" / "report" / "photo.jpg"
        photo.parent.mkdir(parents=True, exist_ok=True)
        photo.write_bytes(b"fake-jpeg-bytes")

    def test_authenticated_backoffice_page_is_no_store(self):
        self.client.force_login(self.user)
        for name in ("backoffice:home", "backoffice:case-list", "backoffice:client-list"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                cache_control = response["Cache-Control"]
                self.assertIn("no-store", cache_control)
                self.assertIn("private", cache_control)

    def test_anonymous_response_is_left_alone(self):
        response = self.client.get(reverse("backoffice:case-list"))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("no-store", response.get("Cache-Control", ""))

    def test_media_keeps_its_own_immutable_cache_control(self):
        self.client.force_login(self.user)
        response = self.client.get("/media/cases/1/report/photo.jpg")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Cache-Control"], "private, max-age=31536000, immutable"
        )

    def test_media_404_is_not_rewritten(self):
        self.client.force_login(self.user)
        response = self.client.get("/media/cases/1/report/missing.jpg")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("no-store", response.get("Cache-Control", ""))


@override_settings(DEBUG=False)
class ErrorTemplateTests(TestCase):
    """UI-L3: branded 403/404/500 pages with a way back."""

    def test_404_uses_project_template(self):
        self.client.force_login(_staff())
        response = self.client.get("/backoffice/does-not-exist-qa/")
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "404.html")
        self.assertContains(response, 'href="/backoffice/"', status_code=404)
        self.assertContains(response, 'id="main" tabindex="-1"', status_code=404)

    def test_404_for_anonymous_visitor_renders(self):
        response = self.client.get("/backoffice/does-not-exist-qa/")
        # The backoffice may bounce anonymous visitors to sign-in first;
        # either way nothing may crash.
        self.assertIn(response.status_code, (302, 404))

    def test_403_uses_project_template(self):
        non_staff = get_user_model().objects.create_user(
            username="qa_outsider", password="test-pass-123", is_staff=False
        )
        self.client.force_login(non_staff)
        response = self.client.get(reverse("backoffice:case-list"))
        self.assertEqual(response.status_code, 403)
        self.assertTemplateUsed(response, "403.html")
        self.assertContains(response, 'href="/backoffice/"', status_code=403)

    def test_500_is_self_contained(self):
        response = server_error(RequestFactory().get("/backoffice/"))
        self.assertEqual(response.status_code, 500)
        body = response.content.decode()
        self.assertIn('href="/backoffice/"', body)
        self.assertIn("Server error", body)
        # No stylesheet or script to fail while the server is already failing.
        self.assertNotIn("<link", body)
        self.assertNotIn("<script", body)


class ShellMarkupTests(TestCase):
    """UI-L6 skip-link target, UI-L7 favicon, UI-M4 responsive tables."""

    @classmethod
    def setUpTestData(cls):
        cls.user = _staff()

    def setUp(self):
        self.client.force_login(self.user)

    def test_main_is_focusable_skip_link_target(self):
        response = self.client.get(reverse("backoffice:home"))
        self.assertContains(response, 'href="#main"')
        self.assertContains(response, '<main id="main" tabindex="-1"')

    def test_favicon_link_in_head(self):
        response = self.client.get(reverse("backoffice:home"))
        self.assertContains(response, 'rel="icon"')
        self.assertContains(response, "img/favicon.svg")

    def test_favicon_file_exists(self):
        from django.contrib.staticfiles import finders

        self.assertIsNotNone(finders.find("img/favicon.svg"))

    def test_list_tables_scroll_inside_their_card(self):
        for name in (
            "backoffice:case-list",
            "backoffice:client-list",
            "backoffice:section-service",
            "backoffice:section-storage",
            "backoffice:section-transport",
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '<div class="table-responsive">')

    def test_triage_table_stacks_on_phones(self):
        statuses = build_service_workflow()
        case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=statuses["diagnostics"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="QA Contact",
        )
        response = self.client.get(reverse("backoffice:case-triage", args=[case.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "table-stack-sm")
        self.assertContains(response, '<div class="table-responsive">')

    def test_list_tables_stack_on_phones(self):
        # One row each, or there is no cell to carry a label.
        statuses = build_service_workflow()
        Case.objects.create(
            case_type=CaseType.SERVICE,
            status=statuses["new_report"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="QA Contact",
        )
        Client.objects.create(name="QA Client")
        for name in ("backoffice:case-list", "backoffice:client-list"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertContains(response, "table-stack-sm")
                # Up to eleven columns at 390px: each cell carries its
                # header as a label.
                self.assertContains(response, 'data-label="')

    def test_storage_intake_price_table_scrolls_inside_its_card(self):
        # No price bands exist here, so every card renders its empty row;
        # the wrapper div is what is under test, not the rows.
        statuses = build_workflow(CaseType.STORAGE, ["inquiry"])
        case = Case.objects.create(
            case_type=CaseType.STORAGE,
            status=statuses["inquiry"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="QA Contact",
        )
        response = self.client.get(reverse("backoffice:storage-intake", args=[case.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<div class="table-responsive">')


class InlineEditTableTests(TestCase):
    """The five inline-editable configuration grids: compact rows on a
    desktop, stacked label/value blocks below 768px."""

    CONFIG_PAGES = (
        "backoffice:parts-catalog",
        "backoffice:section-service-pricing",
        "backoffice:section-storage-pricing",
        "backoffice:section-transport-pricing",
        "backoffice:approval-limits",
    )

    @classmethod
    def setUpTestData(cls):
        cls.manager = _staff("qa_manager", role="MANAGER")
        Part.objects.create(code="QA-1", name="QA part", stock_qty=1)

    def setUp(self):
        self.client.force_login(self.manager)

    def test_config_grids_are_compact_and_stack(self):
        for name in self.CONFIG_PAGES:
            with self.subTest(name=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "table-edit")
                self.assertContains(response, "table-stack-md")

    def test_parts_rows_label_their_cells_and_hide_the_model_help_text(self):
        response = self.client.get(reverse("backoffice:parts-catalog"))
        self.assertContains(response, 'data-label="')
        self.assertContains(response, '<div class="row-actions">')
        # Part.unit_price has a help_text; inside the grid (row forms are
        # prefixed part-<pk>) it is for assistive tech only, never a
        # paragraph inside an 8rem cell...
        self.assertContains(response, 'class="form-text visually-hidden"')
        self.assertNotContains(
            response, '<div class="form-text" id="id_part-'
        )
        # ...while the add form below the grid, which has visible labels,
        # keeps showing it.
        self.assertContains(
            response, '<div class="form-text" id="id_new-unit_price_helptext"'
        )

    def test_storage_bands_explain_blank_cells_once(self):
        # The band fields' model help_text ("Empty = ...") is hidden inside
        # the grid, so the page says it once under the tables instead.
        response = self.client.get(reverse("backoffice:section-storage-pricing"))
        self.assertContains(response, 'id="bands-blank-note"')
        self.assertContains(response, "Empty = open-ended top band.")
        self.assertContains(response, "Empty = not offered for this combination.")
