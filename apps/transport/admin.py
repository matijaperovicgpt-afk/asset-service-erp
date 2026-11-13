"""Admin registrations for the transport app."""

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError

from . import services
from .models import (
    DeparturePickupSlot,
    StandardDepartureDay,
    TransportOrder,
    TransportPriceMatrix,
)


class TransportOrderForm(forms.ModelForm):
    """TRP-05: the departure-day choices come from the active
    ``StandardDepartureDay`` configuration rows, not from code."""

    class Meta:
        model = TransportOrder
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["standard_departure_day"] = forms.ChoiceField(
            choices=[("", "---------")] + services.active_departure_day_choices(),
            required=False,
            label=TransportOrder._meta.get_field(
                "standard_departure_day"
            ).verbose_name,
        )

    def clean(self):
        cleaned_data = super().clean()
        # Normalize the empty choice to NULL (the model field is nullable).
        if not cleaned_data.get("standard_departure_day"):
            cleaned_data["standard_departure_day"] = None
        return cleaned_data


@admin.register(StandardDepartureDay)
class StandardDepartureDayAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "weekday", "is_active")
    list_editable = ("is_active",)


@admin.register(TransportOrder)
class TransportOrderAdmin(admin.ModelAdmin):
    form = TransportOrderForm
    list_display = (
        "case",
        "asset_model",
        "destination_zone",
        "mode",
        "carrier_price",
        "preferred_date",
        "standard_departure_day",
        "estimated_cost",
        "suggested_price",
        "delivered_at",
    )
    readonly_fields = ("suggested_price",)

    @admin.display(description="suggested price (price list)")
    def suggested_price(self, obj):
        if obj.pk is None:
            return "-"
        price = services.price_for(obj)
        return price if price is not None else "-"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        warning = services.validate_mode(obj)
        if warning:
            messages.warning(request, warning)
        try:
            day_warning = services.validate_departure_day(obj)
        except ValidationError as exc:
            # The form already constrains choices; this catches stale
            # values on rows saved before a day was deactivated.
            messages.error(request, "; ".join(exc.messages))
        else:
            if day_warning:
                messages.warning(request, day_warning)
        slot_warning = services.validate_pickup_slot(obj)
        if slot_warning:
            messages.warning(request, slot_warning)


@admin.register(TransportPriceMatrix)
class TransportPriceMatrixAdmin(admin.ModelAdmin):
    list_display = (
        "model_code",
        "brand",
        "destination_zone",
        "price",
        "currency",
        "is_active",
    )
    list_filter = ("destination_zone", "brand", "is_active")
    list_editable = ("price", "is_active")
    search_fields = ("model_code", "brand")


@admin.register(DeparturePickupSlot)
class DeparturePickupSlotAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "origin", "capacity", "status")
    list_filter = ("status",)
    list_editable = ("status",)
