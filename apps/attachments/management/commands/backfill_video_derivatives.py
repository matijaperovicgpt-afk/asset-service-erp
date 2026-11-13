"""Generate missing poster frames and browser-playable copies for videos.

Two jobs, one operator entry point:

* the **poster frame** stored in ``Attachment.thumbnail``, so a gallery of
  videos is not a wall of black rectangles (new uploads get this in
  ``attach_file``; this fills the gap for older rows and for anything
  uploaded while ffmpeg was missing);
* the **browser-playable H.264/AAC MP4** written *beside* the original,
  because a phone ``.mov`` is served as ``video/quicktime``, which Chrome
  and Firefox refuse to play whatever H.264 is inside it.

This command is **the only way to produce transcodes on a deployment
without a Celery broker** - i.e. production today. Transcoding a 50 MB
clip is minutes of ffmpeg, so ``enqueue_video_derivatives`` refuses to run
it on a request path and stamps the row ``pending`` instead; the backlog
that leaves behind is drained from here. Posters are unaffected - they are
cheap enough to run inline at upload time.

    python manage.py backfill_video_derivatives [--dry-run] [--limit N]
            [--posters-only] [--transcodes-only] [--retry-failed]

Safe to re-run: rows that already carry a derivative are excluded by the
queryset, and ``video_status`` keeps ``skipped`` (already playable) and
``failed`` (corrupt) rows out of every later run, so a nightly job cannot
re-burn the multi-minute transcode timeout on the same broken file.
INV-14 holds throughout - the uploaded original is only ever read, never
modified, renamed or deleted.

Synchronous by design, even when a Celery broker is configured: an
operator running a batch repair wants the count when the command returns,
not thousands of queued jobs. It therefore calls ``generate_poster`` and
``transcode_video`` directly rather than ``enqueue_video_derivatives``.
Run it when the worker is idle - the two can otherwise pick the same row,
and the loser's copy is orphaned (harmless, but wasted disk).
"""

from django.core.management.base import BaseCommand

from apps.attachments import video
from apps.attachments.models import Attachment
from apps.attachments.services import generate_poster, transcode_video


class Command(BaseCommand):
    help = "Generate missing poster frames and browser-playable copies for video attachments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many videos are missing each derivative without writing any.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help=(
                "Process at most N videos per derivative (0 = all). Bounds one run's "
                "CPU and disk use; re-run to continue where it left off."
            ),
        )
        parser.add_argument(
            "--posters-only",
            action="store_true",
            help="Extract poster frames only; never enter the transcode path.",
        )
        parser.add_argument(
            "--transcodes-only",
            action="store_true",
            help="Write browser-playable copies only; never enter the poster path.",
        )
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help=(
                "Also retry videos previously stamped 'failed' (resetting them to "
                "'pending' first). Rows stamped 'skipped' stay excluded - those are "
                "already playable everywhere and need no copy."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = max(0, options["limit"])
        do_posters = not options["transcodes_only"]
        do_transcodes = not options["posters_only"]

        if not do_posters and not do_transcodes:
            self.stderr.write(
                self.style.ERROR("--posters-only and --transcodes-only are mutually exclusive.")
            )
            return

        # Fail once, up front, instead of failing identically on every row:
        # CI and any image built before ffmpeg was added to the Dockerfile
        # land here, and so does a deliberate VIDEO_FFMPEG_BIN="" kill
        # switch. No row is touched, so nothing is stamped 'failed' for a
        # reason that has nothing to do with the file.
        if not self._binaries_available(needs_ffprobe=do_transcodes):
            return

        poster_candidates = self._poster_queryset()
        transcode_candidates = self._transcode_queryset(options["retry_failed"])
        poster_total = poster_candidates.count() if do_posters else 0
        transcode_total = transcode_candidates.count() if do_transcodes else 0

        if dry_run:
            self.stdout.write(f"{poster_total} video(s) missing a poster frame.")
            self.stdout.write(f"{transcode_total} video(s) missing a browser-playable copy.")
            return

        posters = unreadable = 0
        if do_posters:
            posters, unreadable = self._run_posters(poster_candidates, limit)

        copies = compatible = failed = deferred = 0
        if do_transcodes:
            if options["retry_failed"]:
                # Clear the sentinel before doing the work, so a run
                # interrupted halfway leaves those rows retryable rather
                # than stuck on a stale 'failed'.
                transcode_candidates.filter(video_status=Attachment.VideoStatus.FAILED).update(
                    video_status=Attachment.VideoStatus.PENDING
                )
            copies, compatible, failed, deferred = self._run_transcodes(
                transcode_candidates, limit
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Posters created: {posters}; unreadable: {unreadable} "
                f"(of {poster_total} candidate video(s)). "
                f"Web copies created: {copies}; already playable: {compatible}; "
                f"failed: {failed}; not attempted: {deferred} "
                f"(of {transcode_total} candidate video(s))."
            )
        )

    def _binaries_available(self, *, needs_ffprobe: bool) -> bool:
        """Refuse to start when the toolchain this command drives is absent."""
        missing = []
        if video.binary("VIDEO_FFMPEG_BIN") is None:
            missing.append("VIDEO_FFMPEG_BIN")
        if needs_ffprobe and video.binary("VIDEO_FFPROBE_BIN") is None:
            missing.append("VIDEO_FFPROBE_BIN")
        if not missing:
            return True
        self.stderr.write(
            self.style.ERROR(
                "ffmpeg is not available ("
                + ", ".join(missing)
                + " is empty or not on PATH); no attachments were touched. Install "
                "ffmpeg - it is in the Docker image, so rebuild if this is a "
                "container - and run this command again."
            )
        )
        return False

    def _poster_queryset(self):
        """Videos with no poster yet.

        Emptiness is the whole state here, exactly as it is for photo
        thumbnails: ``video_status`` describes the MP4 only, so a video
        whose first frame could not be read re-attempts its poster on
        every run - the established behaviour of ``backfill_thumbnails``
        for undecodable photos, and cheap because it is one seek and one
        decoded frame.
        """
        return Attachment.objects.filter(kind=Attachment.Kind.VIDEO, thumbnail="").order_by("pk")

    def _transcode_queryset(self, retry_failed: bool):
        """Videos owed a browser-playable copy.

        ``skipped`` rows are already playable everywhere and must never
        come back - re-probing the whole corpus nightly is pure waste.
        ``failed`` rows are corrupt rather than unlucky, and only
        ``--retry-failed`` brings them back, so a scheduled run cannot
        re-burn VIDEO_TRANSCODE_TIMEOUT on the same file forever. Blank
        and ``pending`` rows are the normal backlog: everything uploaded
        without a broker.
        """
        excluded = [Attachment.VideoStatus.SKIPPED]
        if not retry_failed:
            excluded.append(Attachment.VideoStatus.FAILED)
        return (
            Attachment.objects.filter(kind=Attachment.Kind.VIDEO, web_video="")
            .exclude(video_status__in=excluded)
            .order_by("pk")
        )

    def _run_posters(self, queryset, limit: int):
        created = unreadable = 0
        for attachment in self._limited(queryset, limit):
            if generate_poster(attachment):
                created += 1
            else:
                unreadable += 1
        return created, unreadable

    def _run_transcodes(self, queryset, limit: int):
        created = compatible = failed = deferred = 0
        for attachment in self._limited(queryset, limit):
            if transcode_video(attachment):
                created += 1
                continue
            # transcode_video stamps the row and mirrors the value onto the
            # instance, so the "no" it returned can be reported as the
            # specific kind of "no" it was - which is the difference
            # between "nothing to do" and "come back with more disk".
            if attachment.video_status == Attachment.VideoStatus.SKIPPED:
                compatible += 1
            elif attachment.video_status == Attachment.VideoStatus.FAILED:
                failed += 1
            else:
                deferred += 1
        return created, compatible, failed, deferred

    @staticmethod
    def _limited(queryset, limit: int):
        """Iterate a queryset, honouring ``--limit`` (0 = all).

        ``.iterator()`` keeps a large backlog off the heap; the slice is
        applied first so the LIMIT reaches the database instead of the
        loop.
        """
        if limit:
            queryset = queryset[:limit]
        return queryset.iterator()
