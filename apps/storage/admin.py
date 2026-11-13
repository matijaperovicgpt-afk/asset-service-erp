"""Admin registrations for the storage app."""

import copy

from django import forms
from django.contrib import admin, messages

from . import services
from .models import (
    SitePickup,
    PackageComponent,
    StorageAgreement,
    StorageObstacle,
    StoragePackage,
    StoragePosition,
    StoragePriceBand,
    StorageServiceType,
    StorageTransportService,
    StorageZone,
)


class StoragePositionForm(forms.ModelForm):
    """Surface WNT-06 placement conflicts as form errors, not 500s."""

    class Meta:
        model = StoragePosition
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        hall = cleaned.get("hall")
        row = cleaned.get("row")
        slot = cleaned.get("slot")
        if hall and row and slot:
            services.validate_placement(
                hall,
                row,
                slot,
                cleaned.get("planned_exit_date"),
                exclude_pk=self.instance.pk,
            )
        # WNT-05: a coordinate placement obeys the same bounds/collision
        # rules as ``place_asset``; the admin used to skip them.
        zone = cleaned.get("zone")
        x, y = cleaned.get("pos_x_m"), cleaned.get("pos_y_m")
        agreement = cleaned.get("agreement") or getattr(
            self.instance, "agreement", None
        )
        if zone is not None and x is not None and y is not None and agreement:
            if agreement.asset_length_m is None:
                raise forms.ValidationError(
                    "The agreement has no asset length; record it before "
                    "placing the asset on the yard (WNT-05)."
                )
            length, width = services.asset_footprint_size(agreement)
            services.validate_free_placement(
                zone,
                services.zone_footprint(
                    length, width, x, y, cleaned.get("orientation")
                ),
                cleaned.get("planned_exit_date"),
                exclude_pk=self.instance.pk,
            )
        return cleaned


class StoragePositionInline(admin.TabularInline):
    model = StoragePosition
    form = StoragePositionForm
    extra = 0


class SitePickupInline(admin.StackedInline):
    """WNT rule 4: the site pickup edited alongside its agreement."""

    model = SitePickup
    extra = 0


@admin.register(StoragePosition)
class StoragePositionAdmin(admin.ModelAdmin):
    form = StoragePositionForm
    list_display = ("agreement", "hall", "row", "slot", "planned_exit_date")
    list_filter = ("hall",)

    def save_model(self, request, obj, form, change):
        # WNT-06 enforced at save time as well, in case the form layer is
        # bypassed (e.g. custom admin actions). Coordinate placements have
        # no hall/row/slot; checking them raised "Slot // is occupied".
        if obj.hall and obj.row and obj.slot:
            services.validate_placement(
                obj.hall, obj.row, obj.slot, obj.planned_exit_date, exclude_pk=obj.pk
            )
        super().save_model(request, obj, form, change)


class StoragePriceBandForm(forms.ModelForm):
    """Surface OPEN-05 band overlaps as form errors, not 500s."""

    class Meta:
        model = StoragePriceBand
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        length_min_m = cleaned.get("length_min_m")
        if length_min_m is not None:
            services.validate_band(
                cleaned.get("accommodation"),
                length_min_m,
                cleaned.get("length_max_m"),
                exclude_pk=self.instance.pk,
            )
        return cleaned


@admin.register(StoragePriceBand)
class StoragePriceBandAdmin(admin.ModelAdmin):
    form = StoragePriceBandForm
    list_display = (
        "accommodation",
        "length_min_m",
        "length_max_m",
        "daily_price",
        "monthly_price",
        "seasonal_price",
        "currency",
    )
    list_filter = ("accommodation",)

    def save_model(self, request, obj, form, change):
        # Re-checked at save time as well, in case the form layer is
        # bypassed (the StoragePositionAdmin pattern).
        services.validate_band(
            obj.accommodation, obj.length_min_m, obj.length_max_m, exclude_pk=obj.pk
        )
        super().save_model(request, obj, form, change)


class StorageAgreementForm(forms.ModelForm):
    """The backoffice sanity rules (QA S-M4/S-H4) hold for admin edits too:
    implausible dimensions, and a placed asset that no longer fits."""

    class Meta:
        model = StorageAgreement
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        case = cleaned.get("case")
        if case is None:
            return cleaned
        probe = copy.copy(self.instance)
        probe.case = case
        for name in ("permit_no", "asset_length_m", "asset_width_m", "agreed_price"):
            if name in cleaned:
                setattr(probe, name, cleaned[name])
        stored = (self.instance.permit_no, self.instance.asset_length_m)
        services._validate_agreement_values(probe, stored)
        services._revalidate_yard_position(probe)
        return cleaned


@admin.register(StorageAgreement)
class StorageAgreementAdmin(admin.ModelAdmin):
    form = StorageAgreementForm
    list_display = (
        "case",
        "package",
        "services",
        "ownership",
        "asset_length_m",
        "accommodation",
        "term",
        "agreed_price",
        "suggested_price",
        "period_start",
        "period_end",
        "is_renewal",
    )
    readonly_fields = ("suggested_price",)
    filter_horizontal = ("selected_services",)
    inlines = [StoragePositionInline, SitePickupInline]

    @admin.display(description="selected services")
    def services(self, obj):
        """WNT-02: the list is plural now, so the column joins it."""
        return ", ".join(s.name for s in obj.selected_services.all()) or "-"

    @admin.display(description="suggested price (price list)")
    def suggested_price(self, obj):
        if obj.pk is None:
            return "-"
        price = services.price_for(obj)
        return price if price is not None else "-"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # OPEN-05: an agreed price deviating from the price-list
        # suggestion is fine (ACT-02 keeps pricing a human decision) but
        # is surfaced and logged for the audit trail.
        note = services.record_price_deviation(obj, user=request.user)
        if note is not None:
            messages.info(request, note)


@admin.register(PackageComponent)
class PackageComponentAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "sort_order")
    list_filter = ("is_active",)


@admin.register(StoragePackage)
class StoragePackageAdmin(admin.ModelAdmin):
    list_display = ("name", "season_start", "season_end")
    filter_horizontal = ("components",)


@admin.register(StorageServiceType)
class StorageServiceTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "sort_order")
    list_filter = ("is_active",)

@admin.register(StorageTransportService)
class StorageTransportServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "sort_order")
    list_filter = ("is_active",)


class StorageObstacleInline(admin.TabularInline):
    """WNT-05: the unusable ground in a zone, edited alongside it - the
    lanes and sheds are properties of the yard, not separate records
    anyone would go looking for on their own."""

    model = StorageObstacle
    extra = 0


@admin.register(StorageZone)
class StorageZoneAdmin(admin.ModelAdmin):
    """WNT-05: the yard areas the graphic plan draws. The layout page
    tells staff to "add one in the administration", which was not true
    until this registration existed."""

    list_display = (
        "name",
        "width_m",
        "length_m",
        "clearance_m",
        "is_active",
        "sort_order",
    )
    list_filter = ("is_active",)
    inlines = [StorageObstacleInline]


@admin.register(StorageObstacle)
class StorageObstacleAdmin(admin.ModelAdmin):
    list_display = ("name", "zone", "kind", "pos_x_m", "pos_y_m", "width_m", "length_m")
    list_filter = ("zone", "kind")
