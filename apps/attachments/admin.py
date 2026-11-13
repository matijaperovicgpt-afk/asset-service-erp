"""Admin registrations for the attachments app."""

from django.contrib import admin
from django.contrib.contenttypes.admin import GenericTabularInline

from .models import Attachment


class AttachmentInline(GenericTabularInline):
    """Reusable generic inline for attachments.

    Import this into other apps' admins (cases, intake, ...) to show
    attachments on the parent object's change page.

    # TODO: adopt this inline in the cases/intake admins once those pages
    # are finalized.
    """

    model = Attachment
    extra = 0


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "stage", "content_object", "created_at")
    list_filter = ("kind", "stage")
