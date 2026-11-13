"""Celery tasks for derived media: photo thumbnails, video posters and
browser-playable video copies (NTF-05 infrastructure).

The ``services`` module keeps the single synchronous primitive for each
job - ``generate_thumbnail``, ``generate_poster``, ``transcode_video`` -
because the ``backfill_thumbnails`` and ``backfill_video_derivatives``
management commands call them directly (a batch repair tool should
report its count when it returns, not queue thousands of jobs). Every
task here adds only the primary-key lookup and the "row is gone" case.

Posters and transcodes are two tasks, not one, on purpose: a poster
lands in seconds, and folding it into a job that may spend minutes in
libx264 would leave staff staring at a black rectangle for the whole
encode. They may run concurrently on the same row, which is safe -
their ``update_fields`` sets are disjoint.

Nothing here is retried, matching ``generate_thumbnail_task``: each
primitive already swallows and logs its own failures, the gallery
degrades to the untouched original (INV-14 - derived media is never
authoritative), and the two backfill commands are the idempotent repair
path. A retry would re-burn a multi-minute ffmpeg timeout on a file that
is corrupt rather than unlucky.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="attachments.generate_thumbnail", ignore_result=True)
def generate_thumbnail_task(attachment_id) -> bool:
    """Build the thumbnail for one already-committed ``Attachment``."""
    from apps.attachments.models import Attachment
    from apps.attachments.services import generate_thumbnail

    attachment = Attachment.objects.filter(pk=attachment_id).first()
    if attachment is None:
        logger.info("Attachment %s is gone; no thumbnail needed.", attachment_id)
        return False
    return generate_thumbnail(attachment)


@shared_task(name="attachments.generate_poster", ignore_result=True)
def generate_poster_task(attachment_id) -> bool:
    """Extract the poster frame for one already-committed video ``Attachment``."""
    from apps.attachments.models import Attachment
    from apps.attachments.services import generate_poster

    attachment = Attachment.objects.filter(pk=attachment_id).first()
    if attachment is None:
        logger.info("Attachment %s is gone; no poster needed.", attachment_id)
        return False
    return generate_poster(attachment)


@shared_task(name="attachments.transcode_video", ignore_result=True)
def transcode_video_task(attachment_id) -> bool:
    """Write the browser-playable MP4 for one already-committed ``Attachment``.

    This is the only place a transcode may run in a request-driven
    deployment: it is minutes of ffmpeg, so ``enqueue_video_derivatives``
    never calls the primitive inline (see its docstring). The uploaded
    original is only ever read - the MP4 is written beside it (INV-14).
    """
    from apps.attachments.models import Attachment
    from apps.attachments.services import transcode_video

    attachment = Attachment.objects.filter(pk=attachment_id).first()
    if attachment is None:
        logger.info("Attachment %s is gone; no web copy needed.", attachment_id)
        return False
    return transcode_video(attachment)
