"""Admin registrations for the intake app."""

from django.contrib import admin

from .models import HandoverRecord, IntakeAmendment, IntakeItem, IntakeRecord


#: SVC-07 / INV-12: a signed intake record is final - what it recorded about
#: the asset, and the signature itself, are read-only afterwards. So is
#: the case it belongs to: the TRP-06 / asset-received guards accept
#: *any* signed record on the case, so re-pointing one would let another
#: case pass with no intake record of its own.
_SIGNED_INTAKE_FIELDS = (
    "case",
    "received_by",
    "mount_status",
    "storage_location",
    "cleanliness_status",
    "no_items_received",
    "energy_level",
    "damage_note",
    "general_condition_note",
    "signed_by",
    "signed_at",
)


class IntakeItemInline(admin.TabularInline):
    model = IntakeItem
    extra = 0

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.signed_at is None

    has_add_permission = has_change_permission
    has_delete_permission = has_change_permission


class IntakeAmendmentInline(admin.TabularInline):
    model = IntakeAmendment
    extra = 0
    fields = ("author", "text", "created_at")
    readonly_fields = ("created_at",)
    can_delete = False

    # SVC-07 / INV-12: amendments may be added but never edited.
    def has_change_permission(self, request, obj=None):
        return False


@admin.register(IntakeRecord)
class IntakeRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "received_by", "signed_by", "signed_at", "created_at")
    list_filter = ("mount_status",)
    search_fields = ("storage_location",)
    inlines = [IntakeItemInline, IntakeAmendmentInline]

    def get_readonly_fields(self, request, obj=None):
        # SVC-07 / INV-12: once the record is signed, later-discovered
        # condition goes into amendments, never over the original note.
        readonly = super().get_readonly_fields(request, obj)
        if obj is not None and obj.signed_at is not None:
            readonly = (*readonly, *_SIGNED_INTAKE_FIELDS)
        return readonly


@admin.register(HandoverRecord)
class HandoverRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "handed_over_by", "signed_at", "created_at")
    search_fields = ("received_by_name",)

    def get_readonly_fields(self, request, obj=None):
        # SVC-18: once the receiving signature is captured the record is
        # final, like the intake record above. The worker's signature alone
        # already pins the record to its case and that signature.
        readonly = super().get_readonly_fields(request, obj)
        if obj is not None and obj.signed_at is not None:
            readonly = (*readonly, *(f.name for f in obj._meta.concrete_fields if f.name != "id"))
        elif obj is not None and obj.technician_signed_at is not None:
            readonly = (*readonly, "case", "signed_by_technician", "technician_signed_at")
        return readonly
