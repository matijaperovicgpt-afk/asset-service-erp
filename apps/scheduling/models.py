"""Models for scheduling appointments on cases."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class Appointment(models.Model):
    """A scheduled appointment (intake, handover, transport, storage in/out)."""

    class Kind(models.TextChoices):
        INTAKE = "INTAKE", _("Intake")
        # SVC-03/SVC-04: the technician goes to the asset instead of the
        # asset coming to the yard (an in-water or drive-leg job).
        ON_SITE_VISIT = "ON_SITE_VISIT", _("On-site visit")
        HANDOVER = "HANDOVER", _("Handover")
        TRANSPORT_DEPARTURE = "TRANSPORT_DEPARTURE", _("Transport departure")
        STORAGE_IN = "STORAGE_IN", _("Storage in")
        STORAGE_OUT = "STORAGE_OUT", _("Storage out")

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="appointments",
        verbose_name=_("case"),
    )
    kind = models.CharField(
        max_length=30, choices=Kind.choices, verbose_name=_("kind")
    )
    scheduled_for = models.DateTimeField(verbose_name=_("scheduled for"))
    note = models.TextField(blank=True, verbose_name=_("note"))

    class Meta:
        ordering = ["scheduled_for"]
        verbose_name = _("appointment")
        verbose_name_plural = _("appointments")

    def __str__(self) -> str:
        return f"{self.get_kind_display()} at {self.scheduled_for:%Y-%m-%d %H:%M}"
