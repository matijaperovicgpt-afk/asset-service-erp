"""Admin registrations for the service app."""

from django.contrib import admin

from .models import (
    Diagnostic,
    InspectionCheckItem,
    InspectionPoint,
    InspectionRecord,
    ServicePriceItem,
    TestRun,
    UsedPart,
    WorkLogEntry,
    WorkOrder,
)


class WorkLogEntryInline(admin.TabularInline):
    model = WorkLogEntry
    extra = 0


class UsedPartInline(admin.TabularInline):
    model = UsedPart
    extra = 0

class InspectionCheckItemInline(admin.TabularInline):
    model = InspectionCheckItem
    extra = 0


@admin.register(InspectionPoint)
class InspectionPointAdmin(admin.ModelAdmin):
    list_display = ("label", "system", "is_active", "sort_order")
    list_filter = ("system", "is_active")
    list_editable = ("is_active", "sort_order")
    search_fields = ("label",)

@admin.register(InspectionRecord)
class InspectionRecordAdmin(admin.ModelAdmin):
    list_display = ("pk", "work_order", "performed_by", "signed_by", "signed_at")
    inlines = [InspectionCheckItemInline]

@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("case", "is_preliminary", "signed_by_technician", "signed_at")
    inlines = [WorkLogEntryInline, UsedPartInline]


@admin.register(Diagnostic)
class DiagnosticAdmin(admin.ModelAdmin):
    list_display = (
        "work_order",
        "estimated_parts_price",
        "estimated_completion_date",
        "created_at",
    )


@admin.register(TestRun)
class TestRunAdmin(admin.ModelAdmin):
    list_display = ("work_order", "result", "performed_at")


@admin.register(ServicePriceItem)
class ServicePriceItemAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "category",
        "equipment_category",
        "service_kind",
        "price",
        "unit",
        "is_active",
    )
    list_filter = ("category", "is_active")
    search_fields = ("name", "equipment_category")
