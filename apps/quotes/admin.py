"""Admin registrations for the quotes app."""

from django.contrib import admin

from .models import AdvancePayment, Approval, Quote


class ApprovalInline(admin.TabularInline):
    model = Approval
    extra = 0


class AdvancePaymentInline(admin.TabularInline):
    # AdvancePayment has FKs to both Case and Quote; the Quote FK is
    # nullable, so pin the inline to it explicitly.
    model = AdvancePayment
    fk_name = "quote"
    extra = 0


@admin.register(Quote)
class QuoteAdmin(admin.ModelAdmin):
    list_display = ("id", "case", "amount", "currency", "status", "created_at")
    list_filter = ("status", "currency")
    inlines = [ApprovalInline, AdvancePaymentInline]


@admin.register(AdvancePayment)
class AdvancePaymentAdmin(admin.ModelAdmin):
    # Registered standalone as well, since advance payments may exist
    # without a linked quote.
    list_display = ("id", "case", "quote", "percent", "amount", "due_date", "paid_at")
    list_filter = ("percent",)
