"""
Base Django settings for the erp project.

Environment-specific settings live in ``dev.py`` and ``prod.py``,
both of which import everything from this module.
"""
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import environ
from django.utils.translation import gettext_lazy as _

# BASE_DIR points at the repository root (three levels up from this file).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)

# Read a .env file at the repository root, if present. Never commit .env.
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    environ.Env.read_env(_env_file)

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", default="insecure-dev-only-secret-key")

DEBUG = env("DJANGO_DEBUG")

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# The name shown in the header, page titles, footer and admin. Exposed to
# every template as {{ SITE_NAME }} by config.context_processors.branding.
SITE_NAME = env("SITE_NAME", default="Service Desk")

# Named locations offered by the portal and intake location pickers
# ("Other" with free text is always available as well).
LOCATION_CHOICES = env.list(
    "LOCATION_CHOICES", default=["Main workshop", "Customer site"]
)

# Domestic dialling code (digits, no "+"): numbers without an
# international prefix are read as domestic when matching clients.
PHONE_COUNTRY_CODE = env("PHONE_COUNTRY_CODE", default="44")

# TRP-03: the longest asset (metres) the own transport vehicle may carry;
# anything longer needs an external carrier.
TRANSPORT_OWN_VEHICLE_MAX_LENGTH_M = Decimal(
    env("TRANSPORT_OWN_VEHICLE_MAX_LENGTH_M", default="10")
)

# Storage packages run one season, from this day ("MM-DD") to the same
# day next year.
STORAGE_SEASON_START = tuple(
    int(part) for part in env("STORAGE_SEASON_START", default="01-01").split("-")
)

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "django_htmx",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.clients",
    "apps.cases",
    "apps.intake",
    "apps.quotes",
    "apps.attachments",
    "apps.service",
    "apps.parts",
    "apps.storage",
    "apps.transport",
    "apps.scheduling",
    "apps.notifications",
    "apps.portal",
    "apps.backoffice",
]

# django.contrib.admin goes LAST, after the local apps, and that ordering is
# load-bearing rather than tidy.
#
# The app-directories template loader walks INSTALLED_APPS in order and takes
# the first match. django.contrib.admin ships its own templates/registration/
# - logged_out, password_change_form/done, password_reset_form/done/confirm/
# complete - so with admin listed first it won every one of those names, and
# apps/accounts/templates/registration/ only ever rendered login.html (the one
# name admin does NOT ship, because the admin's own sign-in is admin/login.html).
#
# The effect was that five of the six auth screens rendered in Django admin
# chrome: a staff user who signed out, or reset a password, left the branded
# tool and landed on an unstyled admin page, and the accounts templates sat
# there looking correct while being dead files.
#
# Admin has no ordering requirement of its own - its dependency checks
# (admin.E40x) assert that auth/contenttypes/messages/sessions are INSTALLED,
# not that they precede it - so moving it to the end costs nothing. Nothing
# local ships an admin/* template, so the admin UI itself is unaffected.
INSTALLED_APPS = (
    DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS + ["django.contrib.admin"]
)

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves collected static files (STATIC_ROOT) in production without
    # a separate static server; a no-op under runserver in DEBUG.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Cookie-only locale: LANGUAGE_CODE for everyone, Accept-Language
    # ignored; users switch via the EN/SR buttons (set_language).
    "config.middleware.CookieLocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Signed-in pages get "Cache-Control: no-store" (unless the view set
    # its own, e.g. protected media) so Back after sign-out cannot
    # redisplay client data from the browser cache (QA UI-M6).
    "apps.backoffice.middleware.PrivateNoStoreMiddleware",
    # Annotates request.htmx so views can tell background (htmx) requests
    # from full-page loads.
    "django_htmx.middleware.HtmxMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Client-facing surface: apps.portal (the anonymous fault report at "/"
# and its thank-you page - there is no tracking page in any mode; the
# client is kept informed by e-mail/phone). Off = an internal-only
# deployment where staff enter every case themselves: the portal URLs
# are not mounted at all and "/" sends visitors straight to the sign-in.
# Two urlconfs rather than an ``if`` inside one, so the switch is a plain
# ROOT_URLCONF override in tests and never a module reload.
PORTAL_ENABLED = env.bool("PORTAL_ENABLED", default=True)
ROOT_URLCONF = "config.urls" if PORTAL_ENABLED else "config.urls_internal"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "config.context_processors.branding",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------------------------
# Database (MySQL 8 via mysqlclient)
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": env("DB_NAME", default="erp"),
        "USER": env("DB_USER", default="erp"),
        "PASSWORD": env("DB_PASSWORD", default=""),
        "HOST": env("DB_HOST", default="127.0.0.1"),
        "PORT": env("DB_PORT", default="3306"),
        # Persistent connections. Django's default of 0 opens a fresh TCP
        # connection and MySQL auth handshake for EVERY request; measured
        # against this app's page profile (5 queries on the busy lists,
        # 29 on the rich case detail) that handshake is a meaningful
        # fraction of a cheap page's total time, and it is pure waste.
        #
        # Must stay well below MySQL's wait_timeout (default 28800 s) or
        # Django hands out connections the server has already dropped.
        # CONN_HEALTH_CHECKS covers the remaining race by validating the
        # connection once at the start of each request - without it a
        # MySQL restart makes the first request on every reused
        # connection fail.
        #
        # NB: the live connection count is workers x threads (see
        # docker/gunicorn.conf.py) plus the Celery worker's concurrency,
        # not the worker count. Keep that under MySQL's max_connections
        # (default 151) - the defaults here come to 3 x 4 + 2 = 14.
        "CONN_MAX_AGE": env.int("DB_CONN_MAX_AGE", default=60),
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {
            "charset": "utf8mb4",
        },
    }
}

# ---------------------------------------------------------------------------
# Redis (optional), cache and sessions
# ---------------------------------------------------------------------------

# The single opt-in switch for Redis. Unset - the Windows dev box, CI and
# the test suite - means no Redis anywhere: local-memory cache, database
# sessions and background tasks executed inline at the call site, i.e.
# exactly the synchronous behaviour this app had before Celery existed.
# Set it in the production .env and the same code becomes asynchronous
# with no further changes. Roles are split by database index so that
# operating on one never touches the other: /0 cache, /1 Celery broker.
REDIS_URL = env("REDIS_URL", default="")


def _redis_db(url: str, index: int) -> str:
    """Point a Redis URL at one database index.

    String concatenation would not do: ``redis://redis:6379/0`` already
    names a database, and appending would yield ``.../0/1``.
    """
    return urlunsplit(urlsplit(url)._replace(path=f"/{index}"))


# Used by django-ratelimit (POR-04) among others. Locmem by default;
# REDIS_URL switches this to Redis automatically, and an explicit
# CACHE_URL still wins (memcached, a separate Redis instance, ...).
# NB: django-environ resolves the redis scheme to Django's built-in
# RedisCache only while django-redis is absent from the venv.
CACHES = {
    "default": env.cache_url(
        "CACHE_URL",
        default=_redis_db(REDIS_URL, 0) if REDIS_URL else "locmemcache://",
    ),
}

# Sessions read from the cache and write through to the database. NOT
# the pure "cache" engine: a Redis restart would log every staff user out
# mid-shift, and SESSION_COOKIE_AGE is ten hours. Only with Redis - each
# gunicorn worker has its own locmem, so a logout on one would leave a
# stale session readable from another.
if REDIS_URL:
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/backoffice/"
LOGOUT_REDIRECT_URL = None  # renders registration/logged_out.html

# Sessions expire after a working shift; absolute (no per-request
# refresh) so a stolen cookie has a bounded lifetime.
SESSION_COOKIE_AGE = 10 * 3600

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = env("LANGUAGE_CODE", default="en")

LANGUAGES = [
    ("en", _("English")),
    ("sr-latn", _("Serbian (Latin)")),
]

# Hand-maintained sr_Latn catalogs; compiled django.mo files are
# committed so translation works without gettext installed. To update:
# django-admin makemessages -l sr_Latn && compilemessages (gettext is in
# the Docker image and CI), or the polib fallback documented in README.
LOCALE_PATHS = [BASE_DIR / "locale"]

# The language choice persists for a year (default would be a session
# cookie); set via the SR/EN switcher -> django.views.i18n.set_language.
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365

TIME_ZONE = env("TIME_ZONE", default="UTC")

USE_I18N = True

USE_TZ = True

# ---------------------------------------------------------------------------
# Static files and media
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Uploaded files are organized per case: media/cases/<case_id>/...
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"

# Media serving behind nginx (POR-04 upgrade path): when enabled, the
# protected media view answers with X-Accel-Redirect to an nginx
# ``internal`` location instead of streaming the file from Django.
# See docker/nginx.conf.example.
MEDIA_ACCEL_REDIRECT = env.bool("MEDIA_ACCEL_REDIRECT", default=False)
MEDIA_ACCEL_PREFIX = env("MEDIA_ACCEL_PREFIX", default="/internal-media/")

# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

# NTF-01: staff addresses notified about every new portal submission.
STAFF_NOTIFICATION_EMAILS = env.list("STAFF_NOTIFICATION_EMAILS", default=[])

# ---------------------------------------------------------------------------
# Background jobs (Celery)
# ---------------------------------------------------------------------------

# NTF-05: notification e-mail and thumbnail generation run in a worker
# when a broker exists. Without one every task runs inline at the call
# site - see the enqueue helpers in apps/notifications/services.py and
# apps/attachments/services.py, which branch on ALWAYS_EAGER.
CELERY_BROKER_URL = env(
    "CELERY_BROKER_URL",
    default=_redis_db(REDIS_URL, 1) if REDIS_URL else "",
)
CELERY_TASK_ALWAYS_EAGER = not CELERY_BROKER_URL
# Eager failures are logged, not raised: a broken notification must never
# break the operation that triggered it (the NTF-03 contract). Tests flip
# this on so that a genuine bug inside a task is loud.
CELERY_TASK_EAGER_PROPAGATES = False
# No result backend: nothing calls .get(), and results would consume the
# memory the queue needs (the instance runs noeviction - see
# docker-compose.prod.yml).
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Pillow holds decoded bitmaps; recycling children bounds worker RSS.
CELERY_WORKER_MAX_TASKS_PER_CHILD = 200

# ---------------------------------------------------------------------------
# Video derivatives (ffmpeg)
# ---------------------------------------------------------------------------

# A video attachment gets a poster frame (so a gallery of videos is not a
# wall of black rectangles, and the <video> tag can drop to
# preload="none") and, when the original is not universally playable, a
# browser-friendly H.264/AAC MP4 written *beside* it. The uploaded
# original is never replaced, renamed or deleted: it documents asset
# damage and is the evidentiary record (INV-14).
#
# Both binaries are optional. An empty value, or a name that is not on
# PATH, makes every generator a logged no-op and the gallery keeps
# serving the original - CI has no ffmpeg by design, and neither does an
# image built before this feature. shutil.which resolves these, so an
# absolute path works too.
VIDEO_FFMPEG_BIN = env("VIDEO_FFMPEG_BIN", default="ffmpeg")
VIDEO_FFPROBE_BIN = env("VIDEO_FFPROBE_BIN", default="ffprobe")
# Kill switch for the expensive half only: off still leaves poster frames.
VIDEO_TRANSCODE_ENABLED = env.bool("VIDEO_TRANSCODE_ENABLED", default=True)
# Wall-clock caps passed to subprocess.run. Poster extraction reads a
# single frame and, like thumbnailing, runs inline when there is no
# broker, so its budget must stay tight enough for a request.
VIDEO_PROBE_TIMEOUT = env.int("VIDEO_PROBE_TIMEOUT", default=15)
VIDEO_POSTER_TIMEOUT = env.int("VIDEO_POSTER_TIMEOUT", default=30)
# Transcoding a 50 MB clip (MAX_FILE_SIZE, i.e. ~1 min of phone footage)
# is minutes of CPU, which is why it NEVER runs on a request path - see
# apps/attachments/services.py enqueue_video_derivatives, which skips it
# entirely in eager mode and leaves the backlog to
# `manage.py backfill_video_derivatives`. Keep this well under Celery's
# Redis visibility timeout (3600 s default), or a slow encode is
# redelivered and two ffmpeg processes fight over the same attachment.
VIDEO_TRANSCODE_TIMEOUT = env.int("VIDEO_TRANSCODE_TIMEOUT", default=900)
# x264 threads per job, matching the worker's --concurrency 2. The box
# also runs gunicorn x3 and MySQL; letting libx264 take every core makes
# the whole app crawl while a video is being converted.
VIDEO_TRANSCODE_THREADS = env.int("VIDEO_TRANSCODE_THREADS", default=2)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# Console-only (stdout), container-friendly. Error aggregation is
# Sentry's job when SENTRY_DSN is configured (prod.py).
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"level": "WARNING"},
        # The worker's banner and heartbeat chatter at INFO is noise in
        # the container log; task failures are logged by the app itself.
        "celery": {"level": "WARNING"},
    },
}

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
