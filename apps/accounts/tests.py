from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Role


class UserModelTests(TestCase):
    """Smoke tests for the custom user model."""

    def test_create_user_with_role(self):
        user = get_user_model().objects.create_user(
            username="frontdesk",
            password="test-password-123",
            role=Role.FRONT_DESK,
        )
        self.assertEqual(user.role, Role.FRONT_DESK)
        self.assertTrue(user.check_password("test-password-123"))

    def test_superuser_may_have_blank_role(self):
        admin = get_user_model().objects.create_superuser(
            username="admin",
            password="test-password-123",
        )
        self.assertEqual(admin.role, "")

    def test_a_role_makes_the_account_staff(self):
        """The admin's "Add user" form sets a role but has no "Staff
        status" box; such accounts signed in and then got 403 everywhere."""
        user = get_user_model().objects.create_user(
            username="newhire", password="test-password-123", role=Role.TECHNICIAN
        )
        user.refresh_from_db()
        self.assertTrue(user.is_staff)

        self.client.force_login(user)
        self.assertEqual(self.client.get("/backoffice/cases/").status_code, 200)

    def test_role_given_later_makes_staff_even_with_update_fields(self):
        user = get_user_model().objects.create_user(
            username="later", password="test-password-123"
        )
        self.assertFalse(user.is_staff)
        user.role = Role.FRONT_DESK
        user.save(update_fields=["role"])
        user.refresh_from_db()
        self.assertTrue(user.is_staff)

    def test_no_role_leaves_staff_status_alone(self):
        user = get_user_model().objects.create_user(
            username="norole", password="test-password-123"
        )
        user.refresh_from_db()
        self.assertFalse(user.is_staff)


class AuthFlowTests(TestCase):
    """Staff login, password reset and the rate limit on login POSTs."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="staffer",
            password="test-password-123",
            email="staffer@example.com",
            is_staff=True,
        )

    def test_login_page_renders_and_authenticates(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/accounts/login/",
            {"username": "staffer", "password": "test-password-123"},
        )
        self.assertRedirects(
            response, "/backoffice/", fetch_redirect_response=False
        )

    def test_password_reset_sends_email(self):
        from django.core import mail

        response = self.client.post(
            "/accounts/password_reset/", {"email": "staffer@example.com"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("staffer", mail.outbox[0].body)

    def test_login_rate_limit_returns_403(self):
        from django.test import override_settings

        # A fresh locmem cache so counts from other tests never leak in.
        with override_settings(
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": "ratelimit-test",
                }
            }
        ):
            for _ in range(10):
                response = self.client.post(
                    "/accounts/login/",
                    {"username": "staffer", "password": "wrong"},
                )
                self.assertEqual(response.status_code, 200)
            response = self.client.post(
                "/accounts/login/",
                {"username": "staffer", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 403)

    def test_admin_login_shares_the_rate_limit(self):
        """QA R-M3: /admin/login/ checked the same passwords unthrottled,
        and switching doors must not reset the count."""
        from django.test import override_settings

        with override_settings(
            CACHES={
                "default": {
                    "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                    "LOCATION": "ratelimit-admin-test",
                }
            }
        ):
            for _ in range(5):
                self.client.post(
                    "/accounts/login/",
                    {"username": "staffer", "password": "wrong"},
                )
            for _ in range(5):
                response = self.client.post(
                    "/admin/login/",
                    {"username": "staffer", "password": "wrong"},
                )
                self.assertEqual(response.status_code, 200)
            response = self.client.post(
                "/admin/login/", {"username": "staffer", "password": "wrong"}
            )
            self.assertEqual(response.status_code, 403)


class ProxiedClientIpTests(TestCase):
    """QA R-M4: behind nginx -> docker every peer is the gateway."""

    def test_offices_behind_one_gateway_get_separate_buckets(self):
        from django.test import RequestFactory

        from .views import proxied_client_ip

        factory = RequestFactory()
        a = factory.post("/", HTTP_X_REAL_IP="10.0.0.5", REMOTE_ADDR="172.17.0.1")
        b = factory.post("/", REMOTE_ADDR="172.17.0.1")
        self.assertEqual(proxied_client_ip(a), "10.0.0.5")
        self.assertEqual(proxied_client_ip(b), "172.17.0.1")


class StaffOnlyBackofficeTests(TestCase):
    """An authenticated NON-staff user gets 403 on backoffice views."""

    def test_non_staff_is_forbidden_staff_passes(self):
        User = get_user_model()
        outsider = User.objects.create_user(
            username="outsider", password="demo12345", is_staff=False
        )
        staffer = User.objects.create_user(
            username="insider", password="demo12345", is_staff=True
        )
        self.client.force_login(outsider)
        response = self.client.get("/backoffice/cases/")
        self.assertEqual(response.status_code, 403)

        self.client.force_login(staffer)
        response = self.client.get("/backoffice/cases/")
        self.assertEqual(response.status_code, 200)


class RegistrationTemplateOwnershipTests(TestCase):
    """The branded auth templates must not be shadowed by django.contrib.admin.

    ``django.contrib.admin`` ships its own ``templates/registration/`` -
    logged_out, password_change_form/done, password_reset_form/done/confirm/
    complete - and the app-directories loader takes the first match walking
    INSTALLED_APPS. While admin was listed before the local apps it won every
    one of those names, so signing out or resetting a password dropped the
    user out of the branded shell onto admin chrome, and the templates in
    this app rendered for exactly one URL (login, the one name admin does not
    ship).

    Nothing about that failure is visible: the files exist, the pages return
    200, and only the styling is wrong. Hence a test rather than a comment.
    """

    #: Every name this app means to own.
    OWNED = [
        "registration/login.html",
        "registration/logged_out.html",
        "registration/password_change_form.html",
        "registration/password_change_done.html",
        "registration/password_reset_form.html",
        "registration/password_reset_done.html",
        "registration/password_reset_confirm.html",
        "registration/password_reset_complete.html",
    ]

    def test_accounts_wins_every_registration_template(self):
        from django.template.loader import get_template

        for name in self.OWNED:
            with self.subTest(template=name):
                origin = get_template(name).origin.name.replace("\\", "/")
                self.assertIn(
                    "/apps/accounts/", origin,
                    f"{name} resolved to {origin} instead of this app - "
                    "check that django.contrib.admin still sorts after "
                    "LOCAL_APPS in INSTALLED_APPS.",
                )

    def test_admin_templates_still_belong_to_admin(self):
        """The reorder must not let a local app capture the admin UI."""
        from django.template.loader import get_template

        origin = get_template("admin/base_site.html").origin.name
        self.assertIn("contrib", origin.replace("\\", "/"))
