"""
Client registry: clients (owners/companies) and their assets.

Public case submissions arrive as raw contact/asset text on ``cases.Case``;
during triage the staff match or create the structured ``Client`` /
``Asset`` records defined here.
"""
from django.db import models
from django.utils.translation import gettext_lazy as _


class Client(models.Model):
    """A customer: a private asset owner or a company."""

    name = models.CharField(_("name"), max_length=255)
    phone = models.CharField(_("phone"), max_length=50, blank=True)
    email = models.EmailField(_("email"), blank=True)
    address = models.CharField(_("address"), max_length=255, blank=True)
    is_company = models.BooleanField(_("is company"), default=False)
    notes = models.TextField(_("notes"), blank=True)

    class Meta:
        verbose_name = _("client")
        verbose_name_plural = _("clients")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Asset(models.Model):
    """
    An asset belonging to a client.

    ``length_m`` is required because asset length drives storage
    pricing (price per metre); it must always be captured.

    Procedure §7.2 names six data - name, type, manufacturer, model,
    registration, length. Only the length was ever enforced: it is the
    one NOT NULL column, and the two fields without ``blank=True`` are
    enforced by forms rather than by the database, so the triage path
    that writes ``asset_type=""`` directly gets away with it. The
    columns stay permissive - an asset is often created from half a
    phone call - and ``missing_identification()`` is what the intake record
    asks before anyone signs for the asset.
    """

    client = models.ForeignKey(
        "clients.Client",
        on_delete=models.CASCADE,
        related_name="assets",
        verbose_name=_("client"),
    )
    name = models.CharField(_("name"), max_length=255)
    asset_type = models.CharField(_("asset type"), max_length=100)
    manufacturer = models.CharField(_("manufacturer"), max_length=100, blank=True)
    model = models.CharField(_("model"), max_length=100, blank=True)
    registration_no = models.CharField(_("registration / serial number"), max_length=50, blank=True)
    #: The explicit "this asset carries no registration" answer - a
    #: trailer, a forklift, a machine still waiting for its papers. Without
    #: it, "not registered" and "nobody asked" look identical, which is
    #: the distinction §7 exists to make (the SVC-06 ``no_items_received``
    #: shape).
    is_unregistered = models.BooleanField(
        _("asset is not registered"), default=False
    )
    # Required: length in metres drives storage pricing.
    length_m = models.DecimalField(_("length (m)"), max_digits=5, decimal_places=2)

    class Meta:
        verbose_name = _("asset")
        verbose_name_plural = _("assets")
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.registration_no})" if self.registration_no else self.name

    @property
    def registration_recorded(self) -> bool:
        """§7.2: a registration number, or the fact that there is none."""
        return bool(self.registration_no.strip()) or self.is_unregistered

    def missing_identification(self) -> list:
        """Which of §7.2's six asset data are still unanswered.

        Returned as labels rather than field names, because the only
        consumer is a refusal message a person has to act on.
        """
        missing = []
        if not self.name.strip():
            missing.append(_("name"))
        if not self.asset_type.strip():
            missing.append(_("type"))
        if not self.manufacturer.strip():
            missing.append(_("manufacturer"))
        if not self.model.strip():
            missing.append(_("model"))
        if not self.registration_recorded:
            missing.append(_("registration"))
        if self.length_m is None:
            missing.append(_("length"))
        return missing

    @property
    def is_fully_identified(self) -> bool:
        """§7.2: all six data answered."""
        return not self.missing_identification()
