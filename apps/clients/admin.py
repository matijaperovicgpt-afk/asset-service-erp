"""Admin registrations for the clients app."""
from django.contrib import admin

from .models import Client, Asset


class AssetInline(admin.TabularInline):
    model = Asset
    extra = 0


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email", "is_company")
    list_filter = ("is_company",)
    search_fields = ("name", "phone", "email")
    inlines = [AssetInline]


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("name", "client", "asset_type", "registration_no", "length_m")
    list_filter = ("asset_type",)
    search_fields = ("name", "registration_no", "client__name")
