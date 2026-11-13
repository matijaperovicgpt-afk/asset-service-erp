"""Tests for infrastructure endpoints."""
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from config.settings.base import _redis_db


class HealthzTests(TestCase):
    def test_ok_when_database_answers(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_503_when_database_is_down(self):
        with patch("config.views.connection") as mocked:
            mocked.cursor.side_effect = RuntimeError("db down")
            response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "error"})


class SerbianTranslationTests(TestCase):
    """The committed sr_Latn catalogs actually translate the UI.

    The language comes from the cookie (CookieLocaleMiddleware) - the
    Accept-Language header is deliberately ignored.
    """

    def _use_serbian(self):
        from django.conf import settings

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "sr-latn"

    def test_portal_report_page_renders_in_serbian(self):
        self._use_serbian()
        response = self.client.get("/")
        self.assertContains(response, "Pošalji prijavu")
        self.assertContains(response, "Pošalji prijavu")

    def test_login_page_renders_in_serbian(self):
        self._use_serbian()
        response = self.client.get("/accounts/login/")
        self.assertContains(response, "Korisničko ime")
        self.assertContains(response, "Lozinka")

    def test_english_stays_default_under_test_settings(self):
        response = self.client.get("/accounts/login/")
        self.assertContains(response, "Username")


class CookieLocaleMiddlewareTests(TestCase):
    """Serbian is the default for everyone; only the cookie switches it."""

    @override_settings(LANGUAGE_CODE="sr-latn")
    def test_default_wins_over_accept_language(self):
        response = self.client.get("/", headers={"accept-language": "en"})
        self.assertContains(response, "Pošalji prijavu")

    @override_settings(LANGUAGE_CODE="sr-latn")
    def test_invalid_cookie_falls_back_to_default(self):
        from django.conf import settings

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "xx"
        response = self.client.get("/")
        self.assertContains(response, "Pošalji prijavu")

    @override_settings(LANGUAGE_CODE="sr-latn")
    def test_set_language_switch_persists_and_switches_back(self):
        response = self.client.post(
            "/i18n/setlang/", {"language": "en", "next": "/"}
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/")
        self.assertContains(response, "Submit report")

        self.client.post("/i18n/setlang/", {"language": "sr-latn", "next": "/"})
        response = self.client.get("/")
        self.assertContains(response, "Pošalji prijavu")

    def test_switcher_form_present_on_portal_page(self):
        response = self.client.get("/")
        self.assertContains(response, '/i18n/setlang/')


class CelerySettingsTests(SimpleTestCase):
    """NTF-05: background jobs are opt-in and degrade to running inline.

    """

    def test_eager_without_a_broker(self):
        """No REDIS_URL means no broker, and tasks run at the call site."""
        self.assertEqual(settings.CELERY_BROKER_URL, "")
        self.assertIs(settings.CELERY_TASK_ALWAYS_EAGER, True)

    def test_cache_stays_local_without_redis(self):
        self.assertEqual(
            settings.CACHES["default"]["BACKEND"],
            "django.core.cache.backends.locmem.LocMemCache",
        )

    def test_sessions_stay_on_the_database_without_redis(self):
        """cached_db with per-process locmem would serve stale sessions."""
        self.assertEqual(
            getattr(settings, "SESSION_ENGINE", "django.contrib.sessions.backends.db"),
            "django.contrib.sessions.backends.db",
        )

    def test_app_is_configured_from_django_settings(self):
        """Guards the load-bearing import in config/__init__.py.

        Drop it and @shared_task binds to Celery's own anonymous default
        app, which knows nothing of Django settings - so the no-Redis
        fallback would be silently ignored by any bare .delay().
        """
        from config import celery_app

        self.assertIs(celery_app.conf.task_always_eager, True)

    def test_expected_tasks_are_autodiscovered(self):
        """Discovery must not depend on something else importing first.

        autodiscover_tasks() is lazy, so simply reading app.tasks would
        pass or fail depending on which modules the rest of the suite
        happened to import. import_default_modules() is what a worker
        does on startup, which is the thing worth asserting.
        """
        from config import celery_app

        celery_app.loader.import_default_modules()
        registered = set(celery_app.tasks)
        self.assertIn("notifications.case_transition", registered)
        self.assertIn("notifications.case_created", registered)
        self.assertIn("notifications.new_portal_case", registered)
        self.assertIn("notifications.transport_price_declared", registered)
        self.assertIn("attachments.generate_thumbnail", registered)
        self.assertIn("attachments.generate_poster", registered)
        self.assertIn("attachments.transcode_video", registered)

    def test_redis_roles_are_split_by_database_index(self):
        """String concatenation would produce redis://host:6379/0/1."""
        self.assertEqual(
            _redis_db("redis://redis:6379/0", 1), "redis://redis:6379/1"
        )
        self.assertEqual(_redis_db("redis://redis:6379", 0), "redis://redis:6379/0")
        self.assertEqual(
            _redis_db("redis://:secret@10.0.0.5:6380/3", 1),
            "redis://:secret@10.0.0.5:6380/1",
        )


class GunicornConfigTests(SimpleTestCase):
    """docker/gunicorn.conf.py is deployment-critical and easy to revert.

    It is loaded by path rather than imported: ``docker/`` is not a
    Python package, and gunicorn itself reads it as a file too.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import importlib.util
        from pathlib import Path

        path = Path(settings.BASE_DIR) / "docker" / "gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("gunicorn_conf", path)
        cls.conf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.conf)

    def test_uses_threads_not_the_sync_worker(self):
        """The sync worker caps concurrency at `workers`, which was 3.

        A long download (or any slow client) holds a sync worker for its
        whole duration, so three concurrent video viewers stalled the
        entire application. Reverting this to `sync` silently reinstates
        that.
        """
        self.assertEqual(self.conf.worker_class, "gthread")
        self.assertGreater(self.conf.threads, 1)

    def test_concurrency_is_workers_times_threads(self):
        self.assertGreaterEqual(self.conf.workers * self.conf.threads, 12)

    def test_timeout_outlives_a_slow_media_download(self):
        """Gunicorn's 30 s default kills a worker mid-download.

        With MEDIA_ACCEL_REDIRECT off, Django streams the file itself, and
        a 50 MB video to a phone on site wifi takes longer than that.
        """
        self.assertGreaterEqual(self.conf.timeout, 120)

    def test_workers_are_recycled_to_bound_leaks(self):
        self.assertTrue(self.conf.max_requests)
        self.assertTrue(self.conf.max_requests_jitter)

    def test_every_knob_is_env_tunable(self):
        """An exec-form CMD cannot expand env vars; that is why this file exists."""
        import os
        from importlib import util
        from pathlib import Path

        path = Path(settings.BASE_DIR) / "docker" / "gunicorn.conf.py"
        env = {
            "GUNICORN_WORKERS": "7",
            "GUNICORN_THREADS": "9",
            "GUNICORN_TIMEOUT": "321",
        }
        with patch.dict(os.environ, env):
            spec = util.spec_from_file_location("gunicorn_conf_env", path)
            module = util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.assertEqual(module.workers, 7)
        self.assertEqual(module.threads, 9)
        self.assertEqual(module.timeout, 321)


class DatabaseConnectionReuseTests(SimpleTestCase):
    """Persistent connections, and the bound that makes them safe."""

    def test_base_settings_reuse_connections(self):
        from config.settings import base

        self.assertGreater(base.DATABASES["default"]["CONN_MAX_AGE"], 0)

    def test_health_checks_accompany_reuse(self):
        """Without this, the first request after a MySQL restart fails."""
        from config.settings import base

        self.assertTrue(base.DATABASES["default"]["CONN_HEALTH_CHECKS"])

    def test_reuse_stays_under_mysql_wait_timeout(self):
        from config.settings import base

        self.assertLess(base.DATABASES["default"]["CONN_MAX_AGE"], 28800)

    def test_suite_itself_does_not_reuse(self):
        """--parallel plus a held connection is a hang waiting to happen."""
        self.assertEqual(settings.DATABASES["default"]["CONN_MAX_AGE"], 0)


class PortalSwitchSettingsTests(SimpleTestCase):
    """PORTAL_ENABLED selects the urlconf; the default keeps the portal."""

    def test_default_mounts_the_portal(self):
        from config.settings import base

        self.assertTrue(base.PORTAL_ENABLED)
        self.assertEqual(base.ROOT_URLCONF, "config.urls")

    def test_both_urlconfs_share_the_staff_patterns(self):
        """The internal urlconf is the public one minus the portal."""
        from config import urls, urls_internal

        self.assertEqual(urls.urlpatterns[: len(urls.base_patterns)], urls.base_patterns)
        self.assertEqual(urls_internal.urlpatterns[:-1], urls.base_patterns)


@override_settings(PORTAL_ENABLED=False, ROOT_URLCONF="config.urls_internal")
class InternalDeploymentTests(TestCase):
    """PORTAL_ENABLED=False: staff only, no client-facing surface at all."""

    def test_root_sends_anonymous_visitors_to_the_sign_in(self):
        response = self.client.get("/", follow=True)
        self.assertEqual(response.redirect_chain[0], ("/backoffice/", 302))
        self.assertEqual(response.request["PATH_INFO"], "/accounts/login/")
        self.assertContains(response, "Username")

    def test_root_lands_signed_in_staff_on_the_backoffice(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(
            username="staffer", password="test-pass-123", is_staff=True
        )
        self.client.force_login(user)
        response = self.client.get("/", follow=True)
        self.assertEqual(response.redirect_chain, [("/backoffice/", 302)])
        self.assertEqual(response.status_code, 200)

    def test_portal_routes_do_not_exist(self):
        """404, not 403: nothing reveals a client side this deployment lacks."""
        for url in ("/thanks/", "/track/abc/", "/track/abc/quote/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_sign_in_page_still_renders(self):
        """The shared header must not reverse a portal URL that is unmounted."""
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)

    def test_staff_surfaces_and_probe_are_untouched(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        # Media stays login-protected (POR-04): a redirect, never a file.
        self.assertEqual(self.client.get("/media/cases/1/x.jpg").status_code, 302)
