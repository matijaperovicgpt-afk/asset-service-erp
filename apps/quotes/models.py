"""Models for quotes, approvals and advance payments.

Business documents:
- Quote = quotation / pro-forma invoice for servicing work
- AdvancePayment = advance payment
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Quote(models.Model):
    """Quotation for work on a case."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent")
        ACCEPTED = "accepted", _("Accepted")
        REJECTED = "rejected", _("Rejected")

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="quotes",
        verbose_name=_("case"),
    )
    description = models.TextField(_("description"))
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    currency = models.CharField(_("currency"), max_length=3, default="EUR")
    valid_until = models.DateField(_("valid until"), null=True, blank=True)
    status = models.CharField(
        _("status"),
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("quote")
        verbose_name_plural = _("quotes")

    def __str__(self) -> str:
        return f"Quote #{self.pk} ({self.amount} {self.currency})"


class Approval(models.Model):
    """Approval decision on a quote.

    Internal financial approval ladder: technician -> manager -> director,
    plus client consent (CLIENT level). CLIENT-level approvals have no
    internal user, hence ``decided_by`` is nullable.
    """

    class Level(models.TextChoices):
        CLIENT = "client", _("Client")
        MANAGER = "manager", _("Manager")
        DIRECTOR = "director", _("Director")

    class Decision(models.TextChoices):
        APPROVED = "approved", _("Approved")
        REJECTED = "rejected", _("Rejected")

    quote = models.ForeignKey(
        Quote,
        on_delete=models.CASCADE,
        related_name="approvals",
        verbose_name=_("quote"),
    )
    level = models.CharField(_("level"), max_length=20, choices=Level.choices)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("decided by"),
    )
    decided_at = models.DateTimeField(_("decided at"), null=True, blank=True)
    decision = models.CharField(
        _("decision"), max_length=20, choices=Decision.choices
    )
    note = models.TextField(_("note"), blank=True)

    class Meta:
        verbose_name = _("approval")
        verbose_name_plural = _("approvals")

    def __str__(self) -> str:
        return f"{self.get_level_display()}: {self.get_decision_display()}"


class AdvancePayment(models.Model):
    """Advance payment requested on a case.

    Business rule: 50% advance is required for non-stock parts and for
    transport, hence ``percent`` defaults to 50.
    """

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="advance_payments",
        verbose_name=_("case"),
    )
    quote = models.ForeignKey(
        Quote,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="advance_payments",
        verbose_name=_("quote"),
    )
    percent = models.PositiveIntegerField(_("percent"), default=50)
    amount = models.DecimalField(_("amount"), max_digits=12, decimal_places=2)
    due_date = models.DateField(_("due date"), null=True, blank=True)
    paid_at = models.DateTimeField(_("paid at"), null=True, blank=True)

    class Meta:
        verbose_name = _("advance payment")
        verbose_name_plural = _("advance payments")

    def __str__(self) -> str:
        return f"Advance payment #{self.pk} ({self.percent}%, {self.amount})"
