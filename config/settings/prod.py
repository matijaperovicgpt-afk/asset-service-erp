"""Production settings for the erp project.

Fail-fast policy: required values (SECRET_KEY, ALLOWED_HOSTS) have NO
defaults here - a misconfigured deployment refuses to boot instead of
running with dev credentials.
"""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = False

# Required - no insecure fallback (base.py keeps a dev-only default).
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")

# ---------------------------------------------------------------------------
# Security hardening (behind an HTTPS-terminating proxy, see
# docker/nginx.conf.example)
# ---------------------------------------------------------------------------
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
LANGUAGE_COOKIE_SECURE = True
# Starter value (1 h). Raising to 31536000 + INCLUDE_SUBDOMAINS/PRELOAD
# is a deliberate operator decision once HTTPS is proven stable.
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=3600)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# The login throttle keys on the client IP. Behind nginx -> docker every
# request arrives from the docker gateway, so REMOTE_ADDR made one bucket
# for the whole office and ten bad logins locked everyone out (QA R-M4).
# nginx *overwrites* X-Real-IP with $remote_addr (docker/nginx*.example),
# and gunicorn is published on 127.0.0.1 only, so the header cannot be
# forged from outside. Set to "REMOTE_ADDR" if the app is ever reachable
# without that nginx in front. The default falls back to REMOTE_ADDR when
# the header is absent (a direct hit on 127.0.0.1:8000).
RATELIMIT_IP_META_KEY = env(
    "DJANGO_RATELIMIT_IP_META_KEY", default="apps.accounts.views.proxied_client_ip"
)

# ---------------------------------------------------------------------------
# Static files: hashed + compressed manifests via whitenoise
# (prod only, so tests never need collectstatic).
# ---------------------------------------------------------------------------
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # CompressedManifestStaticFilesStorage, minus the two rules that
        # resolve `sourceMappingURL` comments. The vendored Bootstrap builds
        # reference .map files we deliberately do not ship, and a strict
        # manifest turned that into a collectstatic failure - fatal, because
        # docker/entrypoint.sh runs collectstatic under `set -e`, so the
        # container never came up on a fresh static volume. Everything else
        # stays strict: a missing font still fails the deploy, loudly.
        # See config/staticfiles.py.
        "BACKEND": "config.staticfiles.SourcemapTolerantStaticFilesStorage"
    },
}

# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = env("EMAIL_HOST", default="localhost")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@example.com")
# Bounds the two sends that stay synchronous by design (NTF-05): the
# SVC-17 completion notice in a staff POST and the WNT-03 expiry-reminder
# cron. Without it a hung SMTP server holds the caller for the OS default
# (minutes). Django passes this to the SMTP socket.
EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)

# ---------------------------------------------------------------------------
# Error reporting (optional): set SENTRY_DSN and install sentry-sdk
# (commented line in requirements.txt).
# ---------------------------------------------------------------------------
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    try:
        import sentry_sdk
    except ImportError as exc:  # pragma: no cover - config error path
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "SENTRY_DSN is set but sentry-sdk is not installed; enable the "
            "sentry-sdk[django] line in requirements.txt."
        ) from exc
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=False,
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
    )
