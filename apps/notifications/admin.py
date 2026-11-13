"""Admin registrations for the notifications app."""

from django.contrib import admin

from apps.notifications.models import EmailLog


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    """Read-only-ish admin: e-mail logs are written by the system only."""

    list_display = ("to_address", "subject", "sent_at", "success")
    list_filter = ("success", "sent_at")
    search_fields = ("to_address", "subject")
    readonly_fields = (
        "case",
        "to_address",
        "subject",
        "body",
        "sent_at",
        "success",
        "error",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
