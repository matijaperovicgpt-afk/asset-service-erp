"""Light inventory models supporting the "waiting for part" case status."""

from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Part(models.Model):
    """A spare part or consumable kept in (light) inventory."""

    code = models.CharField(max_length=100, unique=True, verbose_name=_("code"))
    name = models.CharField(max_length=255, verbose_name=_("name"))
    is_consumable = models.BooleanField(
        default=False, verbose_name=_("is consumable")
    )
    stock_qty = models.IntegerField(default=0, verbose_name=_("stock quantity"))
    # SVC-14 (procedure §3.5): what an installed unit of this part costs,
    # so the work actually done can be totalled against the amount the
    # client approved. Nullable because the catalogue predates the field -
    # and a missing price is refused loudly at the gate rather than
    # guessed at, since a part silently counted as free would defeat the
    # comparison it exists for.
    # Never below zero: this is what the SVC-14 total is built from, and a
    # negative unit price would subtract from the work done rather than add
    # to it. Zero stays legal - a part the company does not charge for.
    unit_price = models.DecimalField(
        _("unit price (EUR)"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text=_(
            "Used to total the work done against the approved amount. "
            "A part with no price blocks that check until it is set."
        ),
    )

    class Meta:
        verbose_name = _("part")
        verbose_name_plural = _("parts")

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class PartOrder(models.Model):
    """An order for a part needed by a case.

    While a part order is open (received_at is null) the related case can sit
    in the "waiting for part" status.
    """

    part = models.ForeignKey(
        Part,
        on_delete=models.PROTECT,
        related_name="orders",
        verbose_name=_("part"),
    )
    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="part_orders",
        verbose_name=_("case"),
    )
    # SVC-11 / INV-05: an order for a non-stock part must be backed by a
    # 50% advance payment recorded on the case.
    advance_payment = models.ForeignKey(
        "quotes.AdvancePayment",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="part_orders",
        verbose_name=_("advance payment"),
    )
    ordered_at = models.DateTimeField(
        default=timezone.now, verbose_name=_("ordered at")
    )
    expected_delivery = models.DateField(
        null=True, blank=True, verbose_name=_("expected delivery")
    )
    received_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("received at")
    )

    class Meta:
        verbose_name = _("part order")
        verbose_name_plural = _("part orders")

    def __str__(self) -> str:
        return f"PartOrder #{self.pk} for part {self.part_id}"
