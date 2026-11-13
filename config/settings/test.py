"""Test settings: fast hashing, in-memory email, English UI.

Run the suite with::

    DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test

Database settings are inherited from base (env-driven), so CI points
``DB_HOST``/``DB_NAME``/... at its MySQL service container.
"""
from .base import *  # noqa: F401,F403

# Speed: password hashing dominates auth-heavy view tests otherwise.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# No persistent connections in the suite. Production reuses a connection
# for 60 s (DB_CONN_MAX_AGE), but a test run creates and destroys
# databases and may run --parallel, where a connection held open across
# that is a hang waiting to happen. Deterministic beats fast here.
#
# Rebuilt rather than mutated in place: ``from .base import *`` binds the
# SAME dict object, so ``DATABASES["default"]["CONN_MAX_AGE"] = 0`` would
# reach back and edit ``base.DATABASES`` too - making the production
# value unobservable to anything that imports base afterwards.
DATABASES = {  # noqa: F405
    **DATABASES,  # noqa: F405
    "default": {**DATABASES["default"], "CONN_MAX_AGE": 0},  # noqa: F405
}

# Assertable outbox instead of console noise.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Tests assert English template strings; the sr-latn catalogs would
# otherwise translate them. i18n-specific tests use
# ``translation.override("sr-latn")`` explicitly.
LANGUAGE_CODE = "en"

# The storage tests are written against a mid-October season boundary
# (it exercises the "offer the next season" window in every month), so
# they pin it rather than inherit the env-driven default.
STORAGE_SEASON_START = (10, 15)

# Background jobs never leave the test process, whatever the developer's
# .env says: tasks run inline at the call site (NTF-05), so the suite
# needs no broker. Unlike production, a bug *inside* a task should be
# loud here - the enqueue helpers keep their own try/except, so the
# never-raise contract still holds.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Pinned locmem: a REDIS_URL in the developer's .env would otherwise give
# the suite a shared cache, and POR-04 rate-limit counters would leak
# between runs.
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
}

# ffmpeg must never become a hidden test dependency. It IS installed on
# the Windows dev box and is NOT in CI, so without this pin the existing
# tests that attach fake ``.mp4`` bytes (backoffice, cases, intake,
# portal) would spawn real ffprobe/ffmpeg processes locally and behave
# differently from CI. Empty means "deliberately off" - a clean, logged
# no-op on every path (see apps/attachments/video.py::binary). The tests
# that exercise the machinery override these and mock ``subprocess.run``;
# the one class that genuinely shells out is ``skipUnless``-guarded.
VIDEO_FFMPEG_BIN = ""
VIDEO_FFPROBE_BIN = ""
