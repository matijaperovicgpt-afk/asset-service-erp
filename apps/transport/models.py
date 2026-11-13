"""Models for asset transport logistics."""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class DestinationZone(models.TextChoices):
    """TRP-01: the delivery-zone dimension of the transport price list
    (the transport price list). The price list prices every
    model for two destinations - Zone A and Zone B - so the
    zone, together with the asset model, selects a price."""

    ZONE_A = "ZONE_A", _("Zone A")
    ZONE_B = "ZONE_B", _("Zone B")


class StandardDepartureDay(models.Model):
    """TRP-05 / OPEN-04: the set of standard departure days is data,
    editable by staff in the admin - a fixed set with no season
    dimension (decision 2026-08-01). Seeded Tue/Thu/Sat by
    ``seed_workflow``; deactivate a row instead of deleting it so
    existing orders keep a readable value.
    """

    code = models.CharField(_("code"), max_length=3, unique=True)
    name = models.CharField(_("name"), max_length=30)
    weekday = models.PositiveSmallIntegerField(
        _("weekday"),
        unique=True,
        help_text=_("0 = Monday .. 6 = Sunday (Python date.weekday())."),
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["weekday"]
        verbose_name = _("standard departure day")
        verbose_name_plural = _("standard departure days")

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class TransportOrder(models.Model):
    """Transport order for moving an asset for a case.

    Own vehicle is only suitable for assets up to ~10 m; larger assets
    require an external carrier (TRP-03, soft-validated in services).
    Standard departure days are configuration data
    (``StandardDepartureDay``, TRP-05).
    """

    class Mode(models.TextChoices):
        OWN_VEHICLE = "OWN_VEHICLE", _("Own vehicle")
        EXTERNAL_CARRIER = "EXTERNAL_CARRIER", _("External carrier")

    case = models.OneToOneField(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="transport_order",
        verbose_name=_("case"),
    )
    # TRP-01 "Upit": the asset data the client supplies. Length is kept
    # here (not only on the linked asset) so a phone inquiry with no
    # linked asset yet is still complete; brand/model select the price
    # from the price list (the transport price list).
    asset_brand = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("asset brand"),
        help_text=_("Manufacturer or brand."),
    )
    asset_model = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("asset model"),
        help_text=_("Model code from the price list, e.g. 'M20' (drives pricing)."),
    )
    asset_length_m = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("asset length (m)"),
        help_text=_("Length drives the own-vehicle mode check."),
    )
    # TRP-01: origin site / delivery place / preferred date.
    origin = models.CharField(max_length=255, verbose_name=_("origin site"))
    destination = models.CharField(
        max_length=255, verbose_name=_("delivery place")
    )
    destination_zone = models.CharField(
        max_length=10,
        choices=DestinationZone.choices,
        blank=True,
        default="",
        verbose_name=_("destination zone"),
        help_text=_("Delivery zone - drives the price suggestion."),
    )
    preferred_date = models.DateField(
        null=True, blank=True, verbose_name=_("desired date")
    )
    pickup_slot = models.ForeignKey(
        "DeparturePickupSlot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
        verbose_name=_("pickup slot"),
        help_text=_("The chosen available pickup time."),
    )
    # TRP-01 "offer with the scope of service and estimated costs".
    scope_of_service = models.TextField(
        blank=True,
        verbose_name=_("scope of service"),
        help_text=_("The service scope stated in the offer."),
    )
    estimated_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("estimated cost"),
        help_text=_("Estimated transport cost stated in the offer."),
    )
    mode = models.CharField(
        max_length=20, choices=Mode.choices, verbose_name=_("mode")
    )
    carrier_name = models.CharField(
        max_length=255, blank=True, verbose_name=_("carrier name")
    )
    carrier_price = models.DecimalField(
        max_digits=8,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("carrier price"),
        help_text=_(
            "TRP-03: price of the found external carrier for an asset "
            "over ~10 m (no standard price list applies)."
        ),
    )
    carrier_price_declared_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("carrier price declared at")
    )
    standard_departure_day = models.CharField(
        max_length=3,
        null=True,
        blank=True,
        verbose_name=_("standard departure day"),
        help_text=_("Code of a StandardDepartureDay row."),
    )
    delivered_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("delivered at")
    )
    delivery_note = models.TextField(blank=True, verbose_name=_("delivery note"))

    class Meta:
        verbose_name = _("transport order")
        verbose_name_plural = _("transport orders")

    def __str__(self) -> str:
        return f"TransportOrder for case {self.case_id}"


class LoadingPlan(models.Model):
    """TRP-02 "Loading and unloading plan": the loading/unloading plan agreed
    with the client during consultation, before the asset is moved.

    Records where the asset is set down, lifting equipment availability,
    site conditions, who attends the handover and the agreed
    loading/unloading times. Attaches to the ``TransportOrder`` (the transport root
    record) so the plan lives with the order it belongs to.
    """

    class CraneAvailability(models.TextChoices):
        AVAILABLE = "AVAILABLE", _("Available")
        TO_ARRANGE = "TO_ARRANGE", _("To be arranged")
        NOT_NEEDED = "NOT_NEEDED", _("Not needed")

    transport_order = models.OneToOneField(
        TransportOrder,
        on_delete=models.CASCADE,
        related_name="loading_plan",
        verbose_name=_("transport order"),
    )
    unloading_location = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("unloading location"),
        help_text=_("Where the asset is set down."),
    )
    lifting_availability = models.CharField(
        max_length=10,
        choices=CraneAvailability.choices,
        default=CraneAvailability.TO_ARRANGE,
        verbose_name=_("lifting equipment availability"),
        help_text=_("Whether lifting equipment is available."),
    )
    lifting_notes = models.TextField(
        blank=True,
        verbose_name=_("crane notes"),
        help_text=_("Crane details - operator, site crane, capacity."),
    )
    site_conditions = models.TextField(
        blank=True,
        verbose_name=_("site conditions"),
        help_text=_("Conditions at the delivery site."),
    )
    attendee = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("attendee"),
        help_text=_("Person present at the handover."),
    )
    loading_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("loading time"),
        help_text=_("Agreed loading time."),
    )
    unloading_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("unloading time"),
        help_text=_("Agreed unloading time."),
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))

    class Meta:
        verbose_name = _("loading plan")
        verbose_name_plural = _("loading plans")

    def __str__(self) -> str:
        return f"LoadingPlan for transport order {self.transport_order_id}"


class TransportPriceMatrix(models.Model):
    """TRP-01: the transport price list (the transport price list).

    The price list prices each asset model for two delivery zones (Zone A
    and Zone B). A row is one ``(model_code, destination_zone)``
    combination; ``price`` NULL means the model is priced individually
    ("negotiable") and gives no automatic suggestion - the same convention as
    ``storage.StoragePriceBand`` (NULL price = not offered). Rows are
    data: staff maintain them in the admin/price-list page, seeded from the
    PDF by ``seed_workflow``.
    """

    brand = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("brand"),
        help_text=_("Manufacturer or brand (informational)."),
    )
    model_code = models.CharField(
        max_length=100,
        verbose_name=_("model code"),
        help_text=_("Matches TransportOrder.asset_model, e.g. 'M20'."),
    )
    destination_zone = models.CharField(
        max_length=10,
        choices=DestinationZone.choices,
        verbose_name=_("destination zone"),
    )
    # A matrix price never runs below zero. On the model rather than on the
    # pricing form so the admin passes it too; zero stays legal, NULL keeps
    # its own meaning ("negotiable", no suggestion).
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("price"),
        help_text=_("Empty = priced individually (negotiable), no suggestion."),
    )
    currency = models.CharField(
        max_length=3, default="EUR", verbose_name=_("currency")
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["model_code", "destination_zone"]
        constraints = [
            models.UniqueConstraint(
                fields=["model_code", "destination_zone"],
                name="uniq_transport_price_model_zone",
            )
        ]
        verbose_name = _("transport price")
        verbose_name_plural = _("transport prices")

    def __str__(self) -> str:
        price = self.price if self.price is not None else _("negotiable")
        return f"{self.model_code} - {self.get_destination_zone_display()}: {price}"


class DeparturePickupSlot(models.Model):
    """TRP-01: a saved record of an available pickup time.

    Staff publish concrete slots (a date, optional time and pickup origin)
    from which a client's desired term is chosen; the chosen slot is linked
    on ``TransportOrder.pickup_slot``. Distinct from ``StandardDepartureDay``
    (TRP-05), which is the recurring-weekday configuration, not a concrete
    dated slot. Deactivate/cancel a slot instead of deleting it so orders
    keep a readable value.
    """

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", _("Available")
        BOOKED = "BOOKED", _("Booked")
        CANCELLED = "CANCELLED", _("Cancelled")

    date = models.DateField(_("date"))
    time = models.TimeField(_("time"), null=True, blank=True)
    origin = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("origin"),
        help_text=_("Pickup location, e.g. 'EU luka' or a site."),
    )
    capacity = models.PositiveSmallIntegerField(
        _("capacity"),
        default=1,
        help_text=_("How many assets this slot can take."),
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.AVAILABLE,
        verbose_name=_("status"),
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))

    class Meta:
        ordering = ["date", "time"]
        verbose_name = _("departure pickup slot")
        verbose_name_plural = _("departure pickup slots")

    def __str__(self) -> str:
        when = self.date.isoformat()
        if self.time is not None:
            when = f"{when} {self.time.strftime('%H:%M')}"
        return f"{when} - {self.origin}" if self.origin else when


class LoadingConfirmation(models.Model):
    """TRP-07 "Loading and carriage": the internal confirmation made AT loading,
    when the asset is craned onto the truck/trailer as needed and the
    carrier and the responsible person verify securing and road-readiness.

    This is an INTERNAL record only. Any notification to the client is
    optional and happens outside the app (a phone call / message) - the
    ``client_notified`` flag just attests that it was done. The
    ``signed_by`` / ``signed_at`` pair is the responsible person's
    sign-off (the ``sign_work_order`` signature convention). Attaches to
    the ``TransportOrder`` like ``LoadingPlan`` (TRP-02).
    """

    class VehicleType(models.TextChoices):
        TRUCK = "TRUCK", _("Truck")
        TRAILER = "TRAILER", _("Trailer")

    class Readiness(models.TextChoices):
        # A progression capturing "securing and road-readiness": nothing
        # verified yet -> secured (tied down) -> ready for the road.
        NOT_READY = "NOT_READY", _("Not ready")
        SECURED = "SECURED", _("Secured")
        READY = "READY", _("Ready for road")

    transport_order = models.OneToOneField(
        TransportOrder,
        on_delete=models.CASCADE,
        related_name="loading_confirmation",
        verbose_name=_("transport order"),
    )
    vehicle_type = models.CharField(
        max_length=10,
        choices=VehicleType.choices,
        default=VehicleType.TRUCK,
        verbose_name=_("vehicle type"),
        help_text=_("Loaded onto a truck or a trailer."),
    )
    readiness = models.CharField(
        max_length=10,
        choices=Readiness.choices,
        default=Readiness.NOT_READY,
        verbose_name=_("readiness"),
        help_text=_(
            "Securing and road-readiness verified at loading "
            "."
        ),
    )
    lifting_equipment_used = models.BooleanField(
        _("crane used"),
        default=False,
        help_text=_("Lifting equipment was used to load the asset."),
    )
    carrier = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("carrier"),
        help_text=_("The carrier present at loading."),
    )
    client_notified = models.BooleanField(
        _("client notified"),
        default=False,
        help_text=_(
            "The client was notified about loading outside the app "
            "(no in-app message is sent)."
        ),
    )
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transport_loading_confirmations",
        verbose_name=_("signed by"),
        help_text=_("The responsible person who signed off."),
    )
    signed_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("signed at")
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("updated at"))

    class Meta:
        verbose_name = _("loading confirmation")
        verbose_name_plural = _("loading confirmations")

    def __str__(self) -> str:
        return f"LoadingConfirmation for transport order {self.transport_order_id}"


class DeliveryConfirmation(models.Model):
    """TRP-08 "Delivery": the delivery protocol made at the destination,
    when unloading is organized and the asset is put into service.

    The owner or an authorized representative attends and, as a rule, a
    company employee accompanies the process and signs the protocol. The
    ``signed_by`` / ``signed_at`` pair is that employee's in-app sign-off
    (the ``LoadingConfirmation`` convention: each save re-attests); the
    physically signed protocol is uploaded as a DOCUMENT attachment on
    this record. Attaches to the ``TransportOrder`` like ``LoadingPlan``
    (TRP-02) and ``LoadingConfirmation`` (TRP-07).
    """

    class AttendeeRole(models.TextChoices):
        OWNER = "OWNER", _("Owner")
        REPRESENTATIVE = "REPRESENTATIVE", _("Authorized representative")

    transport_order = models.OneToOneField(
        TransportOrder,
        on_delete=models.CASCADE,
        related_name="delivery_confirmation",
        verbose_name=_("transport order"),
    )
    unloading_done = models.BooleanField(
        _("unloading done"),
        default=False,
        help_text=_("Unloading was organized at the delivery place."),
    )
    put_into_service = models.BooleanField(
        _("put into service"),
        default=False,
        help_text=_("The asset was put into service at the delivery place."),
    )
    attendee_role = models.CharField(
        max_length=20,
        choices=AttendeeRole.choices,
        default=AttendeeRole.OWNER,
        verbose_name=_("attendee role"),
        help_text=_(
            "Who attended the delivery: the owner or an authorized "
            "representative."
        ),
    )
    attendee_name = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("attendee name"),
        help_text=_("The person present at delivery."),
    )
    attendee_contact = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("attendee contact"),
        help_text=_("Phone or e-mail of the attendee."),
    )
    accompanying_employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transport_deliveries_accompanied",
        verbose_name=_("accompanying employee"),
        help_text=_(
            "The company employee who, as a rule, accompanies the "
            "delivery (zaposleni koji prati postupak)."
        ),
    )
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transport_delivery_confirmations",
        verbose_name=_("signed by"),
        help_text=_("The staff member who signed off the protocol in-app."),
    )
    signed_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("signed at")
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))
    updated_at = models.DateTimeField(auto_now=True, verbose_name=_("updated at"))

    class Meta:
        verbose_name = _("delivery confirmation")
        verbose_name_plural = _("delivery confirmations")

    @property
    def case_id(self):
        # ``attachment_upload_path`` resolves "cases/<id>/<stage>/" through
        # this attribute, filing the protocol scan under the case.
        return self.transport_order.case_id

    def __str__(self) -> str:
        return f"DeliveryConfirmation for transport order {self.transport_order_id}"
