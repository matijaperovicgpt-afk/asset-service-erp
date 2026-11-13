"""Admin registrations for the cases app."""
from django.contrib import admin

from .models import ActivityLog, Case, Status, StatusHistory, StatusTransition


class ActivityLogInline(admin.TabularInline):
    model = ActivityLog
    extra = 0
    fields = ("kind", "author", "text", "created_at")
    readonly_fields = ("created_at",)
    can_delete = False

    # INV-03: entries may be added (GEN-02 phone agreements are entered by
    # hand) but existing rows are append-only.
    def has_change_permission(self, request, obj=None):
        return False


class StatusHistoryInline(admin.TabularInline):
    model = StatusHistory
    extra = 0
    readonly_fields = ("from_status", "to_status", "changed_by", "changed_at", "note")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "case_type",
        "client",
        "asset",
        "status",
        "assigned_technician",
        "work_location",
        "reported_at",
    )
    list_filter = ("case_type", "status", "work_location")
    search_fields = (
        "raw_contact_name",
        "raw_phone",
        "raw_email",
        "raw_asset_info",
        "client__name",
        "asset__name",
    )
    inlines = [ActivityLogInline, StatusHistoryInline]

    def get_readonly_fields(self, request, obj=None):
        # INV-01 / STM-02: an existing case changes status only through
        # transition_case(); the admin may set the initial status on add.
        # The same goes for what the services write with their own rules
        # and audit rows (settlement roles, SVC-19 log, SERVICE-only
        # work location): the admin may read them, not rewrite them.
        readonly = super().get_readonly_fields(request, obj)
        if obj is not None:
            readonly = (
                *readonly,
                "status",
                "work_location",
                "delivery_method",
                "financially_settled",
                "contact_role",
                "contact_name",
                "contact_phone",
                "contact_email",
            )
        return readonly


@admin.register(Status)
class StatusAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "name",
        "case_type",
        "sort_order",
        "is_terminal",
        "notify_client",
    )
    list_filter = ("case_type",)


@admin.register(StatusTransition)
class StatusTransitionAdmin(admin.ModelAdmin):
    list_display = ("from_status", "to_status")
    list_filter = ("from_status__case_type",)


@admin.register(StatusHistory)
class StatusHistoryAdmin(admin.ModelAdmin):
    list_display = ("case", "from_status", "to_status", "changed_by", "changed_at")
    list_filter = ("to_status__case_type",)

    # INV-03: history rows are written only by transition_case().
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ActivityLog)
class ActivityLogAdmin(admin.ModelAdmin):
    list_display = ("case", "kind", "author", "created_at")
    list_filter = ("kind",)
    search_fields = ("text",)

    # INV-03: append-only - adding is allowed (GEN-02), editing is not.
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
