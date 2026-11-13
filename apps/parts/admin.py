"""Admin registrations for the parts app."""

from django.contrib import admin

from .models import Part, PartOrder


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "is_consumable", "stock_qty")
    search_fields = ("code", "name")


@admin.register(PartOrder)
class PartOrderAdmin(admin.ModelAdmin):
    list_display = (
        "part",
        "case",
        "ordered_at",
        "expected_delivery",
        "received_at",
        "advance_payment",
    )
