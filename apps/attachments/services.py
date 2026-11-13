"""Business logic for the attachments app.

The model-level validators on ``Attachment.file`` (allowed extensions,
50 MB size cap) are the single source of truth for uploads (POR-04);
``attach_file()`` runs them via ``full_clean()``. The public portal form
keeps its intentionally stricter per-kind limits on top (10 MB photos,
50 MB videos, 5 files).

Derived media follows one shape throughout: a synchronous primitive
(``generate_thumbnail``, ``generate_poster``, ``transcode_video``,
shared by the Celery tasks and the backfill commands) plus an ``enqueue_*``
helper that decides *where* it runs - in a worker once the transaction
commits, or inline when no broker is configured.

The one asymmetry is deliberate and load-bearing: a thumbnail and a
poster frame take milliseconds and may run inline, a transcode takes
minutes and never runs on a request path (see
``enqueue_video_derivatives``). All of it is subordinate to INV-14 -
derived media is never authoritative, the uploaded original is never
modified, renamed or deleted, and every consumer falls back to it.
"""
import logging
import os
import shutil
import tempfile
from collections import defaultdict

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile, File
from django.db import transaction
from django.db.models import Q

from . import tasks, video
from .imaging import build_thumbnail_bytes
from .models import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS, Attachment

logger = logging.getLogger(__name__)


def _defer(task, pk) -> None:
    """Queue ``task`` with ``pk`` once the current transaction commits.

    The ``.delay()`` deliberately carries its own ``try/except``, because
    it does NOT run inside the caller's: ``transaction.on_commit``
    callbacks fire *after* the COMMIT, so an unreachable broker would
    raise straight into the operation that just successfully saved the
    file. The upload would be committed to the database and to disk and
    the user would still see a 500 - the worst of both outcomes.

    Derived media is a nicety, never a precondition (INV-14): a missing
    thumbnail, poster or web copy degrades to serving the original, and
    ``manage.py backfill_thumbnails`` / ``backfill_video_derivatives``
    repair the backlog once the broker is back. This mirrors
    ``notifications.services._enqueue``, where the same hole was found
    and closed first.
    """
    def _dispatch():
        try:
            task.delay(pk)
        except Exception:  # noqa: BLE001 - must not break a saved upload
            logger.exception(
                "Could not queue %s for attachment %s; run the matching "
                "backfill command once the broker is reachable.",
                getattr(task, "name", task),
                pk,
            )

    transaction.on_commit(_dispatch)

#: Free space required before a transcode is attempted, as a multiple
#: of the original's size: the temp file plus its copy into MEDIA_ROOT,
#: with margin. Media files and MySQL share one volume on this box.
TRANSCODE_FREE_SPACE_FACTOR = 3


def attach_file(
    obj, file, kind: str, stage: str, user=None, original_name: str | None = None
) -> Attachment:
    """
    INV-08: attach a file to a business object (Case, IntakeRecord,
    HandoverRecord, ...) with its stage. Validation happens at the model
    layer (extension, size, kind/stage choices) via ``full_clean()``,
    which raises ``ValidationError`` on refusal; the stored filename is
    a random UUID (POR-04) via ``attachment_upload_path`` and the
    client-supplied name is kept in ``original_name``.
    """
    # The file overrules a kind it cannot be: a PDF tagged as a video used
    # to go through ffmpeg (and be re-probed on every backfill), and a PDF
    # tagged as a photo satisfied the SVC-07 arrival-photo gate. The
    # caller's pick still stands where the file allows it - a JPG filed as
    # a "document" (a photographed delivery note, say) stays a document
    # and does not count as an arrival photo.
    name = original_name or getattr(file, "name", "") or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension in VIDEO_EXTENSIONS:
        kind = Attachment.Kind.VIDEO
    elif kind == Attachment.Kind.VIDEO:
        kind = Attachment.Kind.PHOTO if extension in PHOTO_EXTENSIONS else Attachment.Kind.DOCUMENT
    elif kind == Attachment.Kind.PHOTO and extension not in PHOTO_EXTENSIONS:
        kind = Attachment.Kind.DOCUMENT
    attachment = Attachment(
        content_object=obj,
        kind=kind,
        stage=stage,
        file=file,
        original_name=(original_name or getattr(file, "name", "") or "")[:255],
        uploaded_by=user,
    )
    attachment.full_clean()
    attachment.save()
    enqueue_thumbnail(attachment)
    enqueue_video_derivatives(attachment)
    return attachment


def enqueue_thumbnail(attachment) -> None:
    """Arrange for a freshly saved photo to get its thumbnail.

    Without a broker (the dev box, CI and the test suite) the thumbnail
    is built right here, on the instance the caller is holding - not via
    the task, which would load a *second* copy of the row and leave the
    caller's ``attachment.thumbnail`` empty.

    With a broker the job is deferred to ``transaction.on_commit``
    (INV-13). This is mandatory, not defensive: ``create_public_report``
    (POR-01), the backoffice intake view and the storage document
    upload all call ``attach_file`` inside ``transaction.atomic()``, and
    a worker handed the primary key before that commit would find no row.

    The cheap guards are repeated here so a PDF upload does not queue a
    job at all; ``generate_thumbnail`` re-checks them worker-side.
    """
    if attachment.kind != Attachment.Kind.PHOTO or attachment.thumbnail:
        return
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True):
        generate_thumbnail(attachment)
        return
    _defer(tasks.generate_thumbnail_task, attachment.pk)


def generate_thumbnail(attachment) -> bool:
    """Create and store a ~300 px JPEG thumbnail for a PHOTO attachment.

    The synchronous primitive, shared by ``attachments.tasks`` and the
    ``backfill_thumbnails`` command. From a request path call
    ``enqueue_thumbnail()`` instead, which decides where this runs.

    Returns ``True`` when a thumbnail was written. It is a no-op (returns
    ``False``) for non-photos, for attachments that already have one, and
    for images Pillow cannot decode. Thumbnailing must never break an
    upload, so any unexpected error is logged and swallowed - the
    attachment simply falls back to serving its full-size original in
    grids until backfilled.
    """
    if attachment.kind != Attachment.Kind.PHOTO or attachment.thumbnail:
        return False
    try:
        with attachment.file.open("rb") as handle:
            data = build_thumbnail_bytes(handle)
        if data is None:
            return False
        attachment.thumbnail.save("thumb.jpg", ContentFile(data), save=False)
        attachment.save(update_fields=["thumbnail"])
        return True
    except Exception:  # pragma: no cover - defensive; upload must survive
        logger.exception("Thumbnail generation failed for attachment %s", attachment.pk)
        return False


def enqueue_video_derivatives(attachment) -> None:
    """Arrange for a freshly saved video to get its poster and web copy.

    The poster half mirrors ``enqueue_thumbnail`` exactly, and for the
    same reason: without a broker it is built right here, on the instance
    the caller is holding, because the task would load a *second* copy of
    the row and leave the caller's ``attachment.thumbnail`` empty. It
    costs ~0.3 s - one seek and one decoded frame.

    The transcode half is deliberately NOT symmetric, and anyone tidying
    that up would ship the bug this branch exists to prevent: transcoding
    a 50 MB clip (MAX_FILE_SIZE, ~1 min of phone footage) is minutes of
    ffmpeg. Running it inline would occupy one of three gunicorn workers
    for that long and make the upload look like it failed - while the
    file is in fact already saved. Production has no ``REDIS_URL`` today,
    so "inline" is the live configuration, not a hypothetical one. There
    is one rule an operator can hold in their head: **no MP4 derivation
    on a request path, ever.** Without a broker the row is stamped
    ``pending`` and ``manage.py backfill_video_derivatives`` produces the
    copies out of band.

    With a broker both jobs are deferred to ``transaction.on_commit``
    (INV-13). This is mandatory, not defensive: ``create_public_report``
    (POR-01), the backoffice intake view and the storage document
    upload all call ``attach_file`` inside ``transaction.atomic()``, and a
    worker handed the primary key before that commit would find no row.
    Two ``.delay()`` calls, so a poster is never held hostage to a
    multi-minute encode; they are safe to run concurrently on the same
    row because their ``update_fields`` sets are disjoint.

    The cheap guards are repeated here so a photo or a PDF never queues a
    video job at all; both primitives re-check them worker-side.
    """
    if attachment.kind != Attachment.Kind.VIDEO:
        return
    eager = getattr(settings, "CELERY_TASK_ALWAYS_EAGER", True)

    if not attachment.thumbnail:
        if eager:
            generate_poster(attachment)
        else:
            _defer(tasks.generate_poster_task, attachment.pk)

    if attachment.web_video:
        return
    if eager:
        _mark_video_status(attachment, Attachment.VideoStatus.PENDING, only_if_blank=True)
        logger.info(
            "No Celery broker: skipping the transcode for attachment %s. Run "
            "`manage.py backfill_video_derivatives` to produce web copies.",
            attachment.pk,
        )
        return
    _defer(tasks.transcode_video_task, attachment.pk)


def _mark_video_status(attachment, status, *, only_if_blank: bool = False) -> None:
    """Record what happened to the *derived MP4* - never to the original.

    Written with ``.update()`` rather than ``.save()`` so it cannot
    clobber a ``thumbnail`` that a concurrently running poster job has
    just written to the same row: the two tasks touch disjoint columns
    only as long as neither of them saves a whole instance.

    ``only_if_blank`` narrows the write to rows that have never been
    examined, so a re-run cannot downgrade a ``ready`` or ``failed`` row
    back to ``pending``. The new value is mirrored onto the passed
    instance when a row was actually updated, so ``attach_file``'s return
    value tells the truth about what is still outstanding.
    """
    filters = {"pk": attachment.pk}
    if only_if_blank:
        filters["video_status"] = ""
    if Attachment.objects.filter(**filters).update(video_status=status):
        attachment.video_status = status


def _local_path(attachment) -> str | None:
    """Filesystem path of an attachment's original, or ``None``.

    ffmpeg needs a real path and this design deliberately does not stage
    a download, so a storage backend that cannot provide one is a clean
    no-op rather than an error.

    The existence check covers one specific production failure: if the
    worker container is started without the ``media_data`` mount
    (docker-compose.prod.yml), every path resolves and nothing is there.
    The gallery keeps serving originals, so the only symptom is this log
    line - hence the wording, which is the difference between a
    five-minute and a two-hour diagnosis.
    """
    try:
        path = attachment.file.path
    except (NotImplementedError, ValueError):
        logger.warning(
            "Storage %s exposes no filesystem path; ffmpeg cannot read attachment %s.",
            type(attachment.file.storage).__name__,
            attachment.pk,
        )
        return None
    if not os.path.exists(path):
        logger.error(
            "Attachment %s: source missing at %s (media volume not mounted?).",
            attachment.pk,
            path,
        )
        return None
    return path


def generate_poster(attachment) -> bool:
    """Extract and store a ~640 px JPEG poster frame for a VIDEO attachment.

    The synchronous primitive, shared by ``attachments.tasks`` and the
    ``backfill_video_derivatives`` command. From a request path call
    ``enqueue_video_derivatives()`` instead, which decides where this
    runs.

    The poster is stored in ``Attachment.thumbnail`` - the same field a
    photo uses, at a larger edge because a video tile is about three
    times wider. That keeps the template branch, the upload-path helper
    and ``protected_media``'s auth check identical for both, at the cost
    of one rule: any "photos missing a thumbnail" query must filter on
    ``kind`` (``backfill_thumbnails`` does).

    Returns ``True`` when a poster was written. It is a no-op (``False``)
    for non-videos, for attachments that already have one, and whenever
    ffmpeg is absent or the first frame cannot be read. Derived media
    must never break an upload, so any unexpected error is logged and
    swallowed - the video simply renders without a poster, exactly as it
    does today.

    ``video_status`` is untouched: that column describes the MP4 only.
    Poster state is ``thumbnail`` empty-or-not, so an unreadable video
    re-attempts its poster on each backfill - the established behaviour
    of ``backfill_thumbnails`` for undecodable photos.
    """
    if attachment.kind != Attachment.Kind.VIDEO or attachment.thumbnail:
        return False
    try:
        path = _local_path(attachment)
        if path is None:
            return False
        data = video.build_poster_bytes(path)
        if data is None:
            return False
        attachment.thumbnail.save("poster.jpg", ContentFile(data), save=False)
        attachment.save(update_fields=["thumbnail"])
        return True
    except Exception:  # pragma: no cover - defensive; upload must survive
        logger.exception("Poster generation failed for attachment %s", attachment.pk)
        return False


def transcode_video(attachment) -> bool:
    """Write a browser-playable H.264/AAC MP4 *beside* a video original.

    The synchronous primitive, shared by ``attachments.tasks`` and the
    ``backfill_video_derivatives`` command. Never call this from a
    request path - it is minutes of ffmpeg; use
    ``enqueue_video_derivatives()``, which refuses to.

    INV-14, and the reason this is not called "convert": the uploaded
    original is never assigned to, deleted or re-saved on any path here.
    A damage video is the evidentiary record of asset condition (INV-08,
    INV-12); the MP4 is an additional file, and the gallery keeps a route
    back to the untouched bytes.

    Returns ``True`` only when a copy was written. Every other outcome is
    ``False`` plus a ``video_status`` saying which kind of "no" it was, so
    a nightly backfill does not re-burn the transcode timeout on the same
    corrupt file forever:

    * ``skipped`` - already playable everywhere, nothing written;
    * ``failed`` - ffprobe found nothing usable, or ffmpeg gave up;
    * ``pending`` - not attempted yet (no ffmpeg, feature off, no disk
      space): retryable, and the backfill will pick it up.

    The output is kept even when it is larger than the original. That is a
    deliberate departure from the discard-if-larger rule photos follow
    (POR-05) and must not be "fixed": the skip rule is playability-only,
    so every file that gets this far was *not reliably playable*, and
    discarding a larger copy would restore exactly the bug this fixes.
    """
    if attachment.kind != Attachment.Kind.VIDEO or attachment.web_video:
        return False
    try:
        if not getattr(settings, "VIDEO_TRANSCODE_ENABLED", True):
            _mark_video_status(attachment, Attachment.VideoStatus.PENDING, only_if_blank=True)
            logger.debug(
                "VIDEO_TRANSCODE_ENABLED is off; attachment %s keeps only its poster.",
                attachment.pk,
            )
            return False
        # Both binaries are resolved before anything is spawned, so an
        # image built without ffmpeg costs one WARNING per process and
        # nothing else.
        if video.binary("VIDEO_FFMPEG_BIN") is None or video.binary("VIDEO_FFPROBE_BIN") is None:
            _mark_video_status(attachment, Attachment.VideoStatus.PENDING, only_if_blank=True)
            return False

        path = _local_path(attachment)
        if path is None:
            # Not the file's fault (unsuitable storage, missing mount):
            # leave the status alone so a repaired deployment retries it.
            return False

        info = video.probe(path)
        if info is None or video.video_stream(info) is None:
            # A file ffprobe cannot read is not one ffmpeg will fix;
            # encoding it would burn the whole timeout to produce nothing.
            _mark_video_status(attachment, Attachment.VideoStatus.FAILED)
            logger.info(
                "Attachment %s has no readable video stream; not transcoding.",
                attachment.pk,
            )
            return False

        if video.is_browser_ready(attachment.file.name, info):
            _mark_video_status(attachment, Attachment.VideoStatus.SKIPPED)
            logger.debug(
                "Attachment %s is already browser-playable; no copy needed.",
                attachment.pk,
            )
            return False

        original_size = os.path.getsize(path)
        if not _has_transcode_headroom(attachment, original_size):
            _mark_video_status(attachment, Attachment.VideoStatus.PENDING, only_if_blank=True)
            return False

        # A TemporaryDirectory, never a NamedTemporaryFile: the latter's
        # open handle blocks ffmpeg on Windows, and the context manager is
        # the single cleanup path for every exit below - success, failure,
        # timeout and the deleted-row case alike.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = os.path.join(tmp_dir, "web.mp4")
            if not video.transcode_to_mp4(path, tmp_path, info):
                _mark_video_status(attachment, Attachment.VideoStatus.FAILED)
                return False

            # Ten minutes of encoding is ample time for a staff member to
            # delete the row, and save(update_fields=...) against a
            # vanished row raises DatabaseError rather than re-inserting -
            # which the blanket handler below would report as an
            # unexpected crash. The 50 ms thumbnail path never had this
            # window.
            if not Attachment.objects.filter(pk=attachment.pk).exists():
                logger.info(
                    "Attachment %s was deleted during the transcode; dropping the output.",
                    attachment.pk,
                )
                return False

            produced = os.path.getsize(tmp_path)
            logger.info(
                "Transcoded attachment %s: %s bytes -> %s bytes (original kept).",
                attachment.pk,
                original_size,
                produced,
            )
            with open(tmp_path, "rb") as handle:
                attachment.web_video.save("web.mp4", File(handle), save=False)
            attachment.video_status = Attachment.VideoStatus.READY
            attachment.save(update_fields=["web_video", "video_status"])
        return True
    except Exception:  # pragma: no cover - defensive; upload must survive
        logger.exception("Video transcoding failed for attachment %s", attachment.pk)
        return False


def _has_transcode_headroom(attachment, original_size: int) -> bool:
    """Is there room on the media volume to write this copy safely?

    MySQL and the media files share one disk here, so a batch backfill
    that fills the volume takes the database down with it. Three times
    the original covers the temp file plus its copy into MEDIA_ROOT with
    margin, and refusing *before* spawning ffmpeg makes a full disk cost
    a log line instead of fifteen minutes of CPU.
    """
    required = TRANSCODE_FREE_SPACE_FACTOR * original_size
    try:
        free = shutil.disk_usage(settings.MEDIA_ROOT).free
    except OSError:  # pragma: no cover - unreadable volume; let ffmpeg decide
        return True
    if free < required:
        logger.error(
            "Not enough free space to transcode attachment %s: %s bytes free, %s required.",
            attachment.pk,
            free,
            required,
        )
        return False
    return True


def attachments_for(obj):
    """All attachments linked to ``obj`` via the generic relation."""
    return Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(obj),
        object_id=obj.pk,
    )


def attachments_for_objects(objects):
    """All attachments linked to any of ``objects``, in a single query.

    A work object's files are spread across the case and its child
    records (the work order, intake/handover records, delivery
    confirmation), each carrying its own generic-relation attachments.
    This unions them so one gallery can show the whole set. ``objects``
    may mix models; each contributes its (content type, pk). Entries that
    are ``None`` or unsaved (no pk) are skipped. Returns an ``Attachment``
    queryset (empty if nothing matches).
    """
    ids_by_ct = defaultdict(list)
    for obj in objects:
        if obj is None or obj.pk is None:
            continue
        ct = ContentType.objects.get_for_model(type(obj))
        ids_by_ct[ct.pk].append(obj.pk)
    if not ids_by_ct:
        return Attachment.objects.none()
    query = Q()
    for ct_pk, ids in ids_by_ct.items():
        query |= Q(content_type_id=ct_pk, object_id__in=ids)
    return Attachment.objects.filter(query)


def group_attachments_by_kind(attachments):
    """Split an iterable of attachments into photos / videos / documents,
    preserving input order within each group. Shaped for the media
    gallery template (``gallery.photos`` etc.); ``total`` is the count
    across all three so the template can show a single empty state.
    """
    photos, videos, documents = [], [], []
    for attachment in attachments:
        if attachment.kind == Attachment.Kind.PHOTO:
            photos.append(attachment)
        elif attachment.kind == Attachment.Kind.VIDEO:
            videos.append(attachment)
        else:
            documents.append(attachment)
    return {
        "photos": photos,
        "videos": videos,
        "documents": documents,
        "total": len(photos) + len(videos) + len(documents),
    }
