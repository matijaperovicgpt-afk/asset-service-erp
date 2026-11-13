"""The no-full-reload (htmx) wiring: every backoffice and portal page is
served with hx-boost so form submits and navigation swap content in the
background instead of reloading the whole site."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class BackofficeHtmxWiringTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="staffer", password="test-pass-123", is_staff=True
        )
        self.client.force_login(user)

    def test_base_page_is_boosted_and_loads_htmx(self):
        response = self.client.get(reverse("backoffice:home"))
        self.assertContains(response, 'hx-boost="true"')
        self.assertContains(response, "vendor/htmx.min.js")
        self.assertContains(response, "js/app.js")

    def test_main_region_swaps_in_place(self):
        # hx-select/hx-target extract #main from the full response, and
        # show:none keeps the user's scroll position on form submits.
        response = self.client.get(reverse("backoffice:home"))
        # Assert the htmx wiring, not the class list beside it: styling
        # changes on <main> are not this test's subject.
        html = response.content.decode()
        start = html.index('<main id="main"')
        main = html[start : html.index(">", start) + 1]
        for attribute in (
            'hx-target="#main"',
            'hx-select="#main"',
            'hx-swap="outerHTML show:none"',
        ):
            self.assertIn(attribute, main)

    def test_logout_and_language_forms_opt_out(self):
        # Logout and language switching intentionally stay full reloads:
        # they change chrome outside the swapped region.
        response = self.client.get(reverse("backoffice:home"))
        self.assertContains(response, 'hx-boost="false"')

    def test_htmx_middleware_annotates_requests(self):
        response = self.client.get(reverse("backoffice:home"), headers={"HX-Request": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.wsgi_request.htmx)


class PortalHtmxWiringTests(TestCase):
    def test_report_page_is_boosted_and_loads_htmx(self):
        response = self.client.get(reverse("portal:report"))
        self.assertContains(response, 'hx-boost="true"')
        self.assertContains(response, "vendor/htmx.min.js")
