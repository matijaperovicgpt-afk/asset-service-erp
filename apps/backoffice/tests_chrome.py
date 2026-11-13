"""The brand chrome, asserted against real rendered HTML.

Every check here failed silently in review at least once: an unstyled header
variant, a Bootstrap-JS control on a page that loads no Bootstrap JS, a hero
leaking into Tier 3, a dropped htmx attribute. None of them break a template
or raise - the page still returns 200 and only looks wrong - so reading the
templates is not enough and these have to render.

The class name says "chrome" rather than "rebrand" on purpose: what is being
pinned is the contract (one header per page, the right variant, no dead
controls, Tier 3 stays plain), not one particular visual design.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.cases.models import Case, CaseType, ReportChannel
from apps.cases.tests import build_service_workflow

# Controls that need Bootstrap's JS bundle. The portal and the sign-in
# page load htmx + app.js only, so any of these there is a dead control.
BS_JS = ["data-bs-toggle", "data-bs-dismiss", "navbar-toggler", "data-bs-target"]


class BrandChromeTests(TestCase):
    def setUp(self):
        self.statuses = build_service_workflow()
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            status=self.statuses["diagnostics"],
            report_channel=ReportChannel.PHONE,
            raw_contact_name="Probe Contact",
        )
        self.staff = get_user_model().objects.create_user(
            username="probe_staff", password="demo12345",
            is_staff=True, role="FRONT_DESK",
        )

    def html(self, url):
        r = self.client.get(url)
        return r.status_code, r.content.decode("utf-8", "replace")

    # ---- tier 3: backoffice -------------------------------------------
    def test_backoffice_ops_variant(self):
        self.client.force_login(self.staff)
        for name in ("backoffice:home", "backoffice:section-service",
                     "backoffice:client-list", "backoffice:case-list"):
            code, body = self.html(reverse(name))
            self.assertEqual(code, 200, name)
            self.assertIn("brand-header--ops", body, name)
            self.assertIn('id="main"', body, name)
            self.assertNotIn("app-navbar", body, name)
            self.assertNotIn("Playfair", body, f"Playfair leaked into {name}")
            self.assertNotIn("brand-hero", body, f"hero leaked into {name}")

    def test_case_detail_renders(self):
        self.client.force_login(self.staff)
        code, body = self.html(
            reverse("backoffice:case-detail", args=[self.case.pk]))
        self.assertEqual(code, 200)
        self.assertIn("brand-header--ops", body)
        self.assertIn("guide-stepper", body)
        self.assertNotIn("Playfair", body)

    def test_backoffice_htmx_contract_intact(self):
        self.client.force_login(self.staff)
        _, body = self.html(reverse("backoffice:home"))
        for attr in ('hx-boost="true"', 'hx-target="#main"',
                     'hx-select="#main"', 'hx-swap="outerHTML show:none"'):
            self.assertIn(attr, body, f"lost {attr}")
        self.assertIn('hx-boost="false"', body, "logout/admin/lang lost opt-out")
        # hx-history-elt keeps the browser-Back snapshot to #main. Without
        # it htmx restores the whole <body>, re-runs the three vendor
        # scripts, and every collapse toggle opens twice and never closes.
        self.assertIn("hx-history-elt", body, "lost hx-history-elt on #main")
        # The header sits outside #main and inherits nothing from it. Left
        # unscoped, its links swap the whole <body> and re-run the vendor
        # scripts - same double-handler bug through the front door.
        self.assertIn('hx-select-oob="#site-header"', body,
                      "header links no longer scoped to #main")
        # app.js reads this to label input it carried across a swap.
        self.assertIn('data-unsaved-hint="', body, "lost the unsaved-input hint")

    # ---- tier 1: sign-in ----------------------------------------------
    def test_login_auth_variant_and_hero(self):
        code, body = self.html(reverse("login"))
        self.assertEqual(code, 200)
        self.assertIn("brand-header--auth", body)
        self.assertIn("brand-hero", body)
        self.assertIn('id="main"', body, "session-expiry swap target lost")
        self.assertIn('name="username"', body)
        self.assertIn('name="password"', body)
        for bad in BS_JS:
            self.assertNotIn(bad, body, f"dead Bootstrap-JS control: {bad}")

    # ---- tier 1: portal landing ---------------------------------------
    def test_portal_luxe_variant_and_hero(self):
        code, body = self.html(reverse("portal:report"))
        self.assertEqual(code, 200)
        self.assertIn("brand-header--luxe", body)
        self.assertIn("brand-hero", body)
        for bad in BS_JS:
            self.assertNotIn(bad, body, f"dead Bootstrap-JS control: {bad}")

    # ---- tier 2: thanks = no hero, no motion --------------------------
    def test_thanks_is_tier_two(self):
        code, body = self.html(reverse("portal:thanks"))
        self.assertEqual(code, 200)
        self.assertIn("brand-header--luxe", body)
        self.assertNotIn("brand-hero", body, "tier-2 page got a hero")
        self.assertNotIn("<video", body, "tier-2 page got video")
        for bad in BS_JS:
            self.assertNotIn(bad, body, f"dead Bootstrap-JS control: {bad}")

    # ---- one header, not two ------------------------------------------
    def test_exactly_one_header_per_page(self):
        self.client.force_login(self.staff)
        for url in (reverse("backoffice:home"), reverse("login"),
                    reverse("portal:report")):
            _, body = self.html(url)
            self.assertEqual(body.count("<header"), body.count("</header>"), url)
            self.assertEqual(body.count("brand-header "), 1, f"{url}: not exactly 1 header")

    # ---- the hero is pure CSS ------------------------------------------
    def test_hero_has_no_media_or_script(self):
        """The gradient hero must not pull in a video, image or script."""
        for url in (reverse("portal:report"), reverse("login")):
            with self.subTest(url=url):
                code, body = self.html(url)
                self.assertEqual(code, 200)
                self.assertNotIn("<video", body)
                self.assertNotIn("hero.js", body)

    def test_site_name_is_rendered(self):
        """SITE_NAME reaches the header on every shell."""
        self.client.force_login(self.staff)
        with self.settings(SITE_NAME="Acme Ops"):
            for url in (reverse("backoffice:home"), reverse("portal:report")):
                with self.subTest(url=url):
                    _, body = self.html(url)
                    self.assertIn("Acme Ops", body)

    def test_language_switcher_has_an_accessible_name(self):
        """The visible label is a two-letter code; alone it announces as
        "EN, button", which says nothing about what the control does."""
        self.client.force_login(self.staff)
        _, body = self.html(reverse("backoffice:home"))
        self.assertIn("aria-label=", body)
        self.assertIn("Switch language to", body)
