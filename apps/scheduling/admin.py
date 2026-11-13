"""Admin registrations for the scheduling app."""

from django.contrib import admin

from .models import Appointment


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("case", "kind", "scheduled_for")
    list_filter = ("kind",)
