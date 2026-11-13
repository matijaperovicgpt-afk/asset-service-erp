"""``manage.py perf_check`` - report which performance switches are live.

Several of this app's performance features are shipped in code but gated
on deployment configuration (``MEDIA_ACCEL_REDIRECT``, ``REDIS_URL``,
ffmpeg in the image). Each one fails *quietly* by design - media still
streams, e-mail still sends, videos keep their originals - which is
correct behaviour but makes "is it actually on?" impossible to answer
by looking at the app.

This command answers it, and is meant to be run right after a deploy::

    docker compose -f docker-compose.prod.yml exec web python manage.py perf_check

It only reads: nothing here changes data or configuration.

Exit codes, so a deploy script can gate on it:
  0  everything consistent (features may be deliberately off)
  1  something is configured but broken - e.g. REDIS_URL is set and the
     broker cannot be reached, which means jobs are being enqueued into
     the void.

Lives in ``backoffice`` because that is the staff/operator-facing app;
it is not worth a new INSTALLED_APPS entry for a single ops command.
"""
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db import connection

OK = "  ok  "
OFF = " off  "
WARN = " WARN "
FAIL = " FAIL "


class Command(BaseCommand):
    help = "Report which performance features are active in this deployment."

    def handle(self, *args, **options):
        self.failed = False
        self.stdout.write("")
        self._media()
        self._database()
        self._redis()
        self._celery()
        self._video()
        self._backlog()
        self.stdout.write("")
        if self.failed:
            self.stdout.write(self.style.ERROR(
                "One or more features are configured but not working (see FAIL)."
            ))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("No inconsistencies found."))

    # -- reporting helpers -------------------------------------------------

    def _section(self, title):
        self.stdout.write(self.style.MIGRATE_HEADING(f"{title}"))

    def _line(self, marker, label, detail=""):
        style = {
            OK: self.style.SUCCESS,
            OFF: self.style.WARNING,
            WARN: self.style.WARNING,
            FAIL: self.style.ERROR,
        }[marker]
        if marker == FAIL:
            self.failed = True
        self.stdout.write(f"  [{style(marker)}] {label:34} {detail}")

    # -- checks ------------------------------------------------------------

    def _media(self):
        self._section("Media serving")
        if getattr(settings, "MEDIA_ACCEL_REDIRECT", False):
            self._line(
                OK, "MEDIA_ACCEL_REDIRECT",
                f"on, prefix={settings.MEDIA_ACCEL_PREFIX!r} "
                "(nginx must have a matching `internal` location)",
            )
        else:
            self._line(
                OFF, "MEDIA_ACCEL_REDIRECT",
                "OFF - Django streams every media byte itself, occupying a "
                "worker thread for the whole download",
            )

    def _database(self):
        self._section("Database")
        age = settings.DATABASES["default"].get("CONN_MAX_AGE", 0)
        if age:
            self._line(OK, "CONN_MAX_AGE", f"{age}s - connections are reused")
        else:
            self._line(
                OFF, "CONN_MAX_AGE",
                "0 - a fresh connection and auth handshake on every request",
            )
        try:
            with connection.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
            self._line(OK, "connectivity", f"{connection.vendor} reachable")
        except Exception as exc:  # pragma: no cover - defensive
            self._line(FAIL, "connectivity", f"{exc.__class__.__name__}: {exc}")

    def _redis(self):
        self._section("Cache / sessions")
        url = getattr(settings, "REDIS_URL", "")
        backend = settings.CACHES["default"]["BACKEND"].rsplit(".", 1)[-1]
        if not url:
            self._line(
                OFF, "REDIS_URL",
                "unset - supported, but see the caveats below",
            )
            self._line(
                WARN, "cache backend", f"{backend} - per-process, NOT shared",
            )
            # The one consequence that is a correctness problem rather
            # than a performance one, and it is easy to miss.
            self._line(
                WARN, "django-ratelimit counters",
                "per worker process: the effective rate limit is "
                "(workers x the configured rate)",
            )
        else:
            self._line(OK, "REDIS_URL", "set")
            self._line(OK, "cache backend", backend)
            try:
                cache.set("perf_check", "1", 5)
                hit = cache.get("perf_check") == "1"
                self._line(
                    OK if hit else FAIL, "cache round-trip",
                    "set/get succeeded" if hit else "value did not come back",
                )
            except Exception as exc:
                self._line(FAIL, "cache round-trip", f"{exc.__class__.__name__}: {exc}")
        self._line(
            OK, "session engine",
            settings.SESSION_ENGINE.rsplit(".", 1)[-1],
        )

    def _celery(self):
        self._section("Background jobs")
        if settings.CELERY_TASK_ALWAYS_EAGER:
            self._line(
                OFF, "Celery broker",
                "eager - tasks run inline in the request; video transcoding "
                "is skipped entirely and needs backfill_video_derivatives",
            )
            return
        self._line(OK, "Celery broker", settings.CELERY_BROKER_URL)
        try:
            from config.celery import app as celery_app

            with celery_app.connection() as conn:
                conn.ensure_connection(max_retries=1, timeout=3)
            self._line(OK, "broker reachable", "connected")
        except Exception as exc:
            self._line(
                FAIL, "broker reachable",
                f"{exc.__class__.__name__}: {exc} - tasks are being enqueued "
                "into the void",
            )
            return
        try:
            replies = celery_app.control.ping(timeout=3) or []
            if replies:
                names = ", ".join(sorted(k for r in replies for k in r))
                self._line(OK, "worker(s) responding", names)
            else:
                self._line(
                    FAIL, "worker(s) responding",
                    "no worker answered - the queue will grow unbounded",
                )
        except Exception as exc:  # pragma: no cover - defensive
            self._line(WARN, "worker ping", f"{exc.__class__.__name__}: {exc}")

    def _video(self):
        self._section("Video derivatives (ffmpeg)")
        from apps.attachments import video

        for setting_name in ("VIDEO_FFMPEG_BIN", "VIDEO_FFPROBE_BIN"):
            configured = (getattr(settings, setting_name, "") or "").strip()
            resolved = video.binary(setting_name)
            if resolved:
                self._line(OK, setting_name, resolved)
            elif not configured:
                self._line(OFF, setting_name, "empty - deliberately switched off")
            else:
                self._line(
                    WARN, setting_name,
                    f"{configured!r} is not on PATH - this image predates "
                    "ffmpeg, or the binary was removed",
                )
        if not getattr(settings, "VIDEO_TRANSCODE_ENABLED", True):
            self._line(OFF, "VIDEO_TRANSCODE_ENABLED", "kill switch is on")

    def _backlog(self):
        self._section("Derived-media backlog")
        from apps.attachments.models import Attachment

        missing_thumbs = Attachment.objects.filter(
            kind=Attachment.Kind.PHOTO, thumbnail="",
        ).count()
        self._line(
            OK if not missing_thumbs else WARN,
            "photos without a thumbnail",
            f"{missing_thumbs}"
            + ("" if not missing_thumbs else "  -> manage.py backfill_thumbnails"),
        )
        pending = Attachment.objects.filter(
            kind=Attachment.Kind.VIDEO,
            video_status=Attachment.VideoStatus.PENDING,
        ).count()
        failed = Attachment.objects.filter(
            kind=Attachment.Kind.VIDEO,
            video_status=Attachment.VideoStatus.FAILED,
        ).count()
        self._line(
            OK if not pending else WARN,
            "videos pending derivation",
            f"{pending}"
            + ("" if not pending
               else "  -> manage.py backfill_video_derivatives"),
        )
        if failed:
            self._line(
                WARN, "videos failed",
                f"{failed}  -> backfill_video_derivatives --retry-failed",
            )
