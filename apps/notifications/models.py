"""Models for the notifications app."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class EmailLog(models.Model):
    """A record of a single outgoing e-mail attempt tied to a case."""

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="email_logs",
        verbose_name=_("case"),
    )
    to_address = models.CharField(_("to address"), max_length=254)
    subject = models.CharField(_("subject"), max_length=255)
    body = models.TextField(_("body"))
    sent_at = models.DateTimeField(_("sent at"), auto_now_add=True)
    success = models.BooleanField(_("success"), default=True)
    error = models.TextField(_("error"), blank=True)

    class Meta:
        verbose_name = _("email log")
        verbose_name_plural = _("email logs")
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return f"{self.to_address} - {self.subject}"
