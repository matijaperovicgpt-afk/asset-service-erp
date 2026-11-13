"""Models for file attachments (photos, videos, documents).

Attachments use a generic relation so they can be linked to a Case, an
IntakeRecord, a HandoverRecord, or any other model that needs files.
"""

from uuid import uuid4

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

#: Maximum allowed upload size in bytes (50 MB - sized for videos; the
#: portal form enforces its stricter 10 MB photo limit itself).
MAX_FILE_SIZE = 50 * 1024 * 1024

ALLOWED_FILE_EXTENSIONS = [
    "jpg",
    "jpeg",
    "png",
    "gif",
    "mp4",
    "mov",
    "pdf",
    "doc",
    "docx",
]

#: Extensions treated as video (drives ``Attachment.Kind`` selection).
VIDEO_EXTENSIONS = {"mp4", "mov"}
#: Extensions treated as photos; the rest of ALLOWED_FILE_EXTENSIONS are documents.
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}


def validate_file_size(file):
    """Reject files larger than ``MAX_FILE_SIZE``."""
    if file.size and file.size > MAX_FILE_SIZE:
        raise ValidationError(
            _("File is too large (max %(max_mb)s MB)."),
            params={"max_mb": MAX_FILE_SIZE // (1024 * 1024)},
        )


def attachment_upload_path(instance, filename):
    """Build the storage path: "cases/<case_id>/<stage>/<random>.<ext>".

    Resolves the case id from the generic target: if the content object is
    a Case, its own pk is used; otherwise, if the object carries a
    ``case_id`` attribute (IntakeRecord, HandoverRecord, ...), that is
    used; otherwise the file lands under "unlinked".

    POR-04: the stored name is a random UUID - client-supplied names
    never reach the filesystem (the original name is kept on the model).
    The extension is taken from the (validated) upload.

    The "unlinked" bucket only applies to objects that are neither a
    Case nor carry a ``case_id`` (IntakeRecord, HandoverRecord); all
    current attachable models resolve to a case directory.
    """
    obj = instance.content_object
    case_id = "unlinked"
    if obj is not None:
        if obj._meta.label_lower == "cases.case":
            case_id = obj.pk
        elif getattr(obj, "case_id", None) is not None:
            case_id = obj.case_id
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    stored_name = uuid4().hex + (f".{extension}" if extension else "")
    return f"cases/{case_id}/{instance.stage}/{stored_name}"


def thumbnail_upload_path(instance, filename):
    """Store a photo's thumbnail beside its original as ``<uuid>_thumb.jpg``.

    Derived from the already-saved original's path so the thumbnail lands
    in the same case/stage directory and inherits the UUID name (POR-04);
    the ``filename`` argument passed by ``FileField.save`` is ignored.
    """
    original = instance.file.name
    base = original.rsplit(".", 1)[0] if "." in original else original
    return f"{base}_thumb.jpg"


def web_video_upload_path(instance, filename):
    """Store a video's browser-playable copy beside its original as
    ``<uuid>_web.mp4``.

    Derived from the already-saved original's path exactly like
    ``thumbnail_upload_path``: the copy inherits the case/stage directory
    and the UUID stem (POR-04) and therefore also inherits
    ``views.protected_media``'s auth check and X-Accel-Redirect. The
    ``filename`` argument passed by ``FileField.save`` is ignored.
    """
    original = instance.file.name
    base = original.rsplit(".", 1)[0] if "." in original else original
    return f"{base}_web.mp4"


class Attachment(models.Model):
    """A file (photo, video or document) attached to a business object."""

    class Kind(models.TextChoices):
        PHOTO = "photo", _("Photo")
        VIDEO = "video", _("Video")
        DOCUMENT = "document", _("Document")

    class Stage(models.TextChoices):
        REPORT = "report", _("Report")
        INTAKE = "intake", _("Intake")
        WORK = "work", _("Work")
        HANDOVER = "handover", _("Handover")

    class VideoStatus(models.TextChoices):
        """State of the *derived MP4* only - never of the original.

        Blank means "never examined": every photo, every document and
        every row that predates this feature. Poster state is not
        tracked here; it is ``thumbnail`` empty-or-not, exactly like a
        photo's. Transitions: "" -> pending -> ready | skipped | failed,
        and FAILED is cleared only by
        ``backfill_video_derivatives --retry-failed``.
        """

        PENDING = "pending", _("Pending")
        READY = "ready", _("Ready")
        SKIPPED = "skipped", _("Skipped")
        FAILED = "failed", _("Failed")

    content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        verbose_name=_("content type"),
    )
    object_id = models.PositiveBigIntegerField(_("object id"))
    content_object = GenericForeignKey("content_type", "object_id")

    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices)
    stage = models.CharField(_("stage"), max_length=20, choices=Stage.choices)
    file = models.FileField(
        _("file"),
        upload_to=attachment_upload_path,
        validators=[
            FileExtensionValidator(allowed_extensions=ALLOWED_FILE_EXTENSIONS),
            validate_file_size,
        ],
    )
    # Server-generated JPEG preview: ~300 px for photo grids (see
    # services.generate_thumbnail, backfilled by backfill_thumbnails) and
    # ~640 px for the ffmpeg poster frame of a video (see
    # services.generate_poster, backfilled by backfill_video_derivatives),
    # which renders in a tile about three times wider. Blank for
    # documents, for photos Pillow cannot decode, and for videos whose
    # first frame could not be read.
    # Because one field now holds both, any future "photos missing a
    # thumbnail" query MUST filter on kind - as backfill_thumbnails
    # already does, and must keep doing.
    thumbnail = models.FileField(
        _("thumbnail"),
        upload_to=thumbnail_upload_path,
        blank=True,
    )
    # INV-14: the uploaded original is NEVER replaced - a damage video is
    # evidence (INV-08, INV-12). This is an additional, browser-playable
    # H.264/AAC copy, written only when the original is not universally
    # playable (see attachments.video.is_browser_ready) - a .mov is
    # served as video/quicktime, which Chrome and Firefox refuse.
    web_video = models.FileField(
        _("web video"),
        upload_to=web_video_upload_path,
        blank=True,
    )
    # Why web_video is empty: owed, deliberately unnecessary, or broken.
    # Without this the backfill command would re-burn the multi-minute
    # transcode timeout on the same corrupt file on every run, and no
    # operator could answer "what is outstanding?" with a query.
    video_status = models.CharField(
        _("video processing"),
        max_length=20,
        choices=VideoStatus.choices,
        blank=True,
        default="",
    )
    # POR-04: stored filenames are randomized; the upload's name lives here.
    original_name = models.CharField(_("original file name"), max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("uploaded by"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("attachment")
        verbose_name_plural = _("attachments")
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} ({self.get_stage_display()}) #{self.pk}"
