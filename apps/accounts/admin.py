from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import ApprovalLimit, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    """Admin for the custom user model, exposing the staff role."""

    fieldsets = DjangoUserAdmin.fieldsets + (
        (_("Role"), {"fields": ("role",)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        (_("Role"), {"fields": ("role",)}),
    )
    list_display = ("username", "email", "first_name", "last_name", "role", "is_staff")
    list_filter = DjangoUserAdmin.list_filter + ("role",)


@admin.register(ApprovalLimit)
class ApprovalLimitAdmin(admin.ModelAdmin):
    """Admin for per-role quote approval limits."""

    list_display = ("role", "max_amount")
    ordering = ("role",)
