"""Models for storage."""

import datetime
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.dateformat import format as date_format
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _


# WNT-04: a package season runs from STORAGE_SEASON_START to the same day
# next year (settings / env, "MM-DD"). Defined here because the model
# validates against it; services.py re-exports these names, since it
# already imports this module and the reverse would be a cycle.
SEASON_START_MONTH, SEASON_START_DAY = settings.STORAGE_SEASON_START


def season_start_label() -> str:
    """The season boundary as people read it, e.g. "1 January"."""
    return date_format(
        datetime.date(2000, SEASON_START_MONTH, SEASON_START_DAY), "j F"
    )


class PackageComponent(models.Model):
    """WNT-04: one service a package arrangement may include (service,
    conservation, storage, ...). Data-driven like ``StorageServiceType``
    so management edits the catalog in the admin, not in code."""

    name = models.CharField(max_length=255, unique=True, verbose_name=_("name"))
    is_active = models.BooleanField(default=True, verbose_name=_("active"))
    sort_order = models.PositiveIntegerField(
        default=0, verbose_name=_("sort order")
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("package component")
        verbose_name_plural = _("package components")

    def __str__(self) -> str:
        return self.name


class StoragePackage(models.Model):
    """A storage package offered for a season.

    The storage season runs from STORAGE_SEASON_START to the same day
    next year.
    """

    name = models.CharField(max_length=255, verbose_name=_("name"))
    season_start = models.DateField(verbose_name=_("season start"))
    season_end = models.DateField(verbose_name=_("season end"))
    components = models.ManyToManyField(
        PackageComponent,
        blank=True,
        related_name="packages",
        verbose_name=_("components"),
        help_text=_("Services this package arrangement includes."),
    )
    included_services = models.TextField(
        blank=True, verbose_name=_("included services")
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))

    def clean(self):
        """WNT-04: a package covers one season, boundary to boundary.

        The dates are data so a package can be corrected without a
        migration, but they are not free: a package season that does not
        run from the configured boundary to the same day the following
        year is not the arrangement the offer describes, and the offer
        text quotes these two dates to the client verbatim.
        """
        super().clean()
        from django.core.exceptions import ValidationError as _VE

        expected = (SEASON_START_MONTH, SEASON_START_DAY)
        boundary = {"date": season_start_label()}
        errors = {}
        if self.season_start and (self.season_start.month, self.season_start.day) != expected:
            errors["season_start"] = gettext(
                "The package season starts on %(date)s (WNT-04)."
            ) % boundary
        if self.season_end and (self.season_end.month, self.season_end.day) != expected:
            errors["season_end"] = gettext(
                "The package season ends on %(date)s (WNT-04)."
            ) % boundary
        if (
            not errors
            and self.season_start
            and self.season_end
            and self.season_end.year != self.season_start.year + 1
        ):
            errors["season_end"] = gettext(
                "The package season runs to %(date)s of the FOLLOWING "
                "year (WNT-04)."
            ) % boundary
        if errors:
            raise _VE(errors)

    class Meta:
        verbose_name = _("storage package")
        verbose_name_plural = _("storage packages")

    def __str__(self) -> str:
        return self.name


class StorageAccommodation(models.TextChoices):
    """Where the asset is stored - the official price list (valid from
    01.10.2025) prices open-yard and indoor-hall storage separately."""

    OPEN = "OPEN", _("Outdoor storage (open yard)")
    INDOOR = "INDOOR", _("Indoor storage (hall)")


class StorageTerm(models.TextChoices):
    """Billing term from the official price list: per day, per month or
    for the whole season."""

    DAILY = "DAILY", _("Daily")
    MONTHLY = "MONTHLY", _("Monthly")
    SEASONAL = "SEASONAL", _("Seasonal")


class StoragePriceBand(models.Model):
    """OPEN-05: base storage price by accommodation and asset
    length band, per the official price list valid from 01.10.2025
    (EUR, excl. VAT; assets up to 14 m - above that the price is
    negotiated manually, no band covers it - no machines).

    A band covers ``[length_min_m, length_max_m)`` - min inclusive, max
    exclusive - within one accommodation; ``length_max_m`` NULL means an
    open-ended top band. Each band carries a price per storage term; a
    NULL price means that combination is not offered (no suggestion).
    The listed price includes lift-out, transport to the yard, rack,
    washing, engine flush and conservation, battery maintenance and
    ventilation. Bands are data - staff edit them in the admin/pricing
    page. Package prices stay in the quote/contract (WNT-04); bands
    price standalone base storage only.
    """

    accommodation = models.CharField(
        max_length=10,
        choices=StorageAccommodation.choices,
        default=StorageAccommodation.OPEN,
        verbose_name=_("accommodation"),
    )
    # A band covers a length range and carries money; neither runs below
    # zero. The floor is on the model rather than on the pricing page so
    # the admin and any future writer pass it too - the same reasoning as
    # the asset-dimension floor. Zero stays legal: a price of 0.00 is a
    # line offered free, which is not the same as NULL ("not offered").
    length_min_m = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("length from (m, inclusive)"),
    )
    length_max_m = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("length to (m, exclusive)"),
        help_text=_("Empty = open-ended top band."),
    )
    daily_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("daily price"),
        help_text=_("Empty = not offered for this combination."),
    )
    monthly_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("monthly price"),
        help_text=_("Empty = not offered for this combination."),
    )
    seasonal_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("seasonal price"),
        help_text=_("Empty = not offered for this combination."),
    )
    currency = models.CharField(
        max_length=3, default="EUR", verbose_name=_("currency")
    )

    class Meta:
        ordering = ["accommodation", "length_min_m"]
        constraints = [
            models.UniqueConstraint(
                fields=["accommodation", "length_min_m"],
                name="uniq_band_accommodation_min",
            )
        ]
        verbose_name = _("storage price band")
        verbose_name_plural = _("storage price bands")

    def price_for_term(self, term):
        """Price for a ``StorageTerm`` value, or ``None`` when the
        combination is not offered."""
        return {
            StorageTerm.DAILY: self.daily_price,
            StorageTerm.MONTHLY: self.monthly_price,
            StorageTerm.SEASONAL: self.seasonal_price,
        }.get(term)

    def __str__(self) -> str:
        upper = self.length_max_m if self.length_max_m is not None else "∞"
        prices = "/".join(
            str(p) if p is not None else "-"
            for p in (self.daily_price, self.monthly_price, self.seasonal_price)
        )
        return (
            f"{self.get_accommodation_display()} [{self.length_min_m} m - "
            f"{upper} m): {prices} {self.currency}"
        )


class StorageServiceType(models.Model):
    """WNT-02 "selected services": the type of storage sold on an
    agreement. Data-driven so future storage variants (covered hall,
    open yard, ...) are added in the admin, not in code."""

    name = models.CharField(max_length=255, unique=True, verbose_name=_("name"))
    is_active = models.BooleanField(default=True, verbose_name=_("active"))
    sort_order = models.PositiveIntegerField(
        default=0, verbose_name=_("sort order")
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("storage service type")
        verbose_name_plural = _("storage service types")

    def __str__(self) -> str:
        return self.name


class StorageAgreement(models.Model):
    """Winter-storage agreement for a case.

    Asset length drives pricing. WNT-02: the agreement records length,
    site location, ownership and the selected service.
    """

    case = models.OneToOneField(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="storage_agreement",
        verbose_name=_("case"),
    )
    package = models.ForeignKey(
        StoragePackage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agreements",
        verbose_name=_("package"),
    )
    # A outline has a positive size or none at all. Without this floor a
    # negative length produced an INVERTED yard footprint (x1 < x0), which
    # passed the bounds test, overlapped nothing, and let two assets be
    # placed on the same square metre - so the floor lives on the model,
    # where every writer passes, rather than on one form.
    asset_length_m = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("asset length (m)"),
        help_text=_("Length drives pricing."),
    )
    asset_width_m = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("asset width (m)"),
    )
    permit_no = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("permit number"),
        help_text=_("Reference number of the operating permit."),
    )
    # WNT-02 (procedure §5.2): "selected services" - plural. A client can
    # take storage AND preservation AND a cleaning without any package,
    # which the single ``service_type`` foreign key this replaces could
    # not represent; the second and third choice simply had nowhere to go.
    # Modelled the way StoragePackage already models its components.
    selected_services = models.ManyToManyField(
        StorageServiceType,
        blank=True,
        related_name="agreements",
        verbose_name=_("selected services"),
        help_text=_("Every service the client has chosen, package aside."),
    )
    origin_site = models.CharField(
        max_length=255, blank=True, verbose_name=_("origin site")
    )
    ownership = models.CharField(
        max_length=255,
        blank=True,
        verbose_name=_("ownership"),
        help_text=_("Registered owner / ownership note."),
    )
    accommodation = models.CharField(
        max_length=10,
        choices=StorageAccommodation.choices,
        blank=True,
        default="",
        verbose_name=_("accommodation"),
        help_text=_("Open yard or indoor hall - drives the price suggestion."),
    )
    term = models.CharField(
        max_length=10,
        choices=StorageTerm.choices,
        blank=True,
        default="",
        verbose_name=_("storage term"),
        help_text=_("Daily, monthly or seasonal - drives the price suggestion."),
    )
    agreed_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("agreed price"),
    )
    # WNT-03/WNT-04: the valid commercial period stated in the quote and
    # contract (packages run from STORAGE_SEASON_START to the same day next year by convention).
    period_start = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("commercial period start"),
    )
    period_end = models.DateField(
        null=True,
        blank=True,
        verbose_name=_("commercial period end"),
    )
    is_renewal = models.BooleanField(
        default=False,
        verbose_name=_("is renewal"),
        help_text=_(
            "WNT-03: this offer renews a package the client already had."
        ),
    )
    prior_agreement = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewals",
        verbose_name=_("prior agreement"),
        help_text=_("The package agreement this one renews."),
    )
    # WNT-03: stamped by the send_storage_expiry_reminders job so the
    # client is reminded once per agreement, not once per cron run. Clear
    # it by hand (or on a renewal) to let the reminder go out again.
    expiry_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("expiry reminder sent at"),
        help_text=_(
            "When the client was reminded that this storage period is "
            "ending. Empty means no reminder has gone out yet."
        ),
    )

    class Meta:
        verbose_name = _("storage agreement")
        verbose_name_plural = _("storage agreements")

    def __str__(self) -> str:
        return f"StorageAgreement for case {self.case_id}"


class StorageZone(models.Model):
    """WNT-05: a physical storage area (yard/hall) drawn to scale for the
    graphic layout ("Raspored u storage").

    A rectangle ``width_m`` (X extent) by ``length_m`` (depth from the
    exit). The exit edge is the front (y = 0) by convention, so an asset
    with a smaller ``pos_y_m`` is nearer the exit. Assets are placed at
    real coordinates and validated for bounds, collisions and exit order
    by ``services.validate_free_placement()`` / ``place_asset()``.
    """

    name = models.CharField(max_length=255, unique=True, verbose_name=_("name"))
    width_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        verbose_name=_("width (m)"),
        help_text=_("Extent along the exit edge (X)."),
    )
    length_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        verbose_name=_("length (m)"),
        help_text=_("Depth away from the exit edge (Y)."),
    )
    # WNT-05 clearance: the working gap that must stay between two outlines.
    # Without it the yard accepted assets flush against each other - legal
    # geometry, useless yard, because nobody can get a stand, a ladder or
    # a pressure washer between them. Per zone, because an indoor hall is
    # racked tighter than open ground. The gap is required between
    # assets only: an asset may still sit against the zone edge, which is
    # a fence, not a neighbour.
    clearance_m = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=Decimal("0.75"),
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("clearance between assets (m)"),
        help_text=_(
            "Minimum working gap kept between two outlines. 0 allows them to "
            "touch."
        ),
    )
    is_active = models.BooleanField(default=True, verbose_name=_("active"))
    sort_order = models.PositiveIntegerField(
        default=0, verbose_name=_("sort order")
    )
    # WNT-09: a site plan, an aerial photo or a hand sketch of the yard,
    # stretched to the zone's extents and drawn under the grid. It is the
    # backdrop the assets are placed over - nothing is measured from it.
    plan_image = models.ImageField(
        upload_to="yard_plans/",
        blank=True,
        verbose_name=_("site plan image"),
        help_text=_(
            "Optional aerial photo or drawing of the yard, stretched to "
            "the width x length above and shown under the plan."
        ),
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("storage zone")
        verbose_name_plural = _("storage zones")

    def __str__(self) -> str:
        return self.name

    @property
    def area_m2(self):
        """Total ground in the zone, for the occupancy readout."""
        return self.width_m * self.length_m



class ObstacleKind(models.TextChoices):
    """What a piece of unusable ground in a zone actually is. The kind is
    only ever shown to staff - every kind blocks placement identically."""

    LANE = "LANE", _("Driving lane / aisle")
    CRANE = "CRANE", _("Crane or lifting track")
    BUILDING = "BUILDING", _("Building / shed")
    RACK = "RACK", _("Fixed rack")
    OTHER = "OTHER", _("Other obstacle")


class StorageObstacle(models.Model):
    """WNT-05: ground inside a zone that no asset may occupy.

    A ``StorageZone`` on its own is a bare rectangle, so the yard plan
    would happily place an asset across the travel-lift lane, on top of the
    shed, or in the turning circle. An obstacle is a permanent
    reservation: drawn hatched on the plan and treated by
    ``services.validate_free_placement()`` as an occupant that never
    leaves, so nothing can be placed on it and nothing can be trapped
    behind it (it is not going anywhere, so exit order does not apply).
    """

    zone = models.ForeignKey(
        StorageZone,
        on_delete=models.CASCADE,
        related_name="obstacles",
        verbose_name=_("zone"),
    )
    name = models.CharField(max_length=255, verbose_name=_("name"))
    kind = models.CharField(
        max_length=20,
        choices=ObstacleKind.choices,
        default=ObstacleKind.OTHER,
        verbose_name=_("kind"),
    )
    pos_x_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("x (m)"),
        help_text=_("Lower-left corner, from the exit-side origin."),
    )
    pos_y_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("y (m)"),
    )
    width_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("width (m)"),
        help_text=_("Extent along the exit edge (X)."),
    )
    length_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name=_("length (m)"),
        help_text=_("Extent away from the exit edge (Y)."),
    )

    class Meta:
        ordering = ["zone", "name"]
        verbose_name = _("storage obstacle")
        verbose_name_plural = _("storage obstacles")

    def __str__(self) -> str:
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def footprint(self) -> tuple:
        """``(x0, y0, x1, y1)`` in metres - the same shape an asset
        footprint has, so the collision test treats both alike."""
        return (
            self.pos_x_m,
            self.pos_y_m,
            self.pos_x_m + self.width_m,
            self.pos_y_m + self.length_m,
        )


class StorageOrientation(models.TextChoices):
    """How an asset's outline is aligned in the yard: bow-to-exit (length
    runs along the depth axis) or broadside (length runs along the exit
    edge)."""

    LENGTHWISE = "LENGTHWISE", _("Lengthwise (bow to exit)")
    CROSSWISE = "CROSSWISE", _("Crosswise (broadside)")


class StoragePosition(models.Model):
    """A physical placement for a stored asset.

    Two coexisting modes:

    * Legacy hall/row/slot placement - exit order enforced by
      ``services.validate_placement()`` / ``assign_position()`` (WNT-06).
    * Coordinate placement in a ``StorageZone`` (``pos_x_m``/``pos_y_m``,
      drawn to scale on the graphic yard) - bounds, collisions and exit
      order enforced by ``services.validate_free_placement()`` /
      ``place_asset()`` (WNT-05).

    In both modes the first asset to leave must stay reachable, so
    ``planned_exit_date`` matters when assigning positions.
    """

    agreement = models.ForeignKey(
        StorageAgreement,
        on_delete=models.CASCADE,
        related_name="positions",
        verbose_name=_("agreement"),
    )
    hall = models.CharField(max_length=50, blank=True, verbose_name=_("hall"))
    row = models.CharField(max_length=10, blank=True, verbose_name=_("row"))
    slot = models.CharField(max_length=10, blank=True, verbose_name=_("slot"))
    # WNT-05: coordinate placement on the graphic yard. NULL zone means a
    # legacy hall/row/slot placement.
    zone = models.ForeignKey(
        StorageZone,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="positions",
        verbose_name=_("zone"),
    )
    pos_x_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("x (m)"),
        help_text=_("Lower-left corner of the footprint, from the exit-side origin."),
    )
    pos_y_m = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("y (m)"),
    )
    orientation = models.CharField(
        max_length=10,
        choices=StorageOrientation.choices,
        default=StorageOrientation.LENGTHWISE,
        verbose_name=_("orientation"),
    )
    planned_exit_date = models.DateField(
        null=True, blank=True, verbose_name=_("planned exit date")
    )

    class Meta:
        verbose_name = _("storage position")
        verbose_name_plural = _("storage positions")

    @property
    def is_coordinate_placed(self) -> bool:
        """True when this is a zone/coordinate placement (WNT-05) rather
        than a legacy hall/row/slot placement (WNT-06)."""
        return (
            self.zone_id is not None
            and self.pos_x_m is not None
            and self.pos_y_m is not None
        )

    def __str__(self) -> str:
        if self.is_coordinate_placed:
            return f"{self.zone} @ ({self.pos_x_m}, {self.pos_y_m}) m"
        return f"{self.hall}/{self.row}/{self.slot}"


class SitePickup(models.Model):
    """WNT rule 4 "Pickup from site": the service organizes pulling
    the asset out and delivering it to the yard.

    Many clients cannot deliver the asset themselves, so the service handles both the
    lift-out and the road delivery from the
    asset's own or another site. The receipt, condition
    inventory and arrival photos are the shared intake
    stack: this pickup points at the ``intake.IntakeRecord`` (with its
    ``IntakeItem`` rows) and the case's ``Attachment`` photos rather than
    duplicating them. The site itself is named on
    ``StorageAgreement.origin_site``; ``is_own_site`` only records
    whether it is the firm's own site or another one.
    """

    class OrganizedBy(models.TextChoices):
        SERVICE = "SERVICE", _("Service organizes pickup")
        CLIENT = "CLIENT", _("Client brings the asset")

    class LiftOutMethod(models.TextChoices):
        CRANE = "CRANE", _("Crane")
        FORKLIFT = "FORKLIFT", _("Forklift")
        MANUAL = "MANUAL", _("Manual / by hand")
        OTHER = "OTHER", _("Other")

    class TransportMode(models.TextChoices):
        # Mirrors transport.TransportOrder.Mode so a transport order can be
        # derived from a pickup later without remapping values.
        OWN_VEHICLE = "OWN_VEHICLE", _("Own vehicle")
        EXTERNAL_CARRIER = "EXTERNAL_CARRIER", _("External carrier")

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        SCHEDULED = "SCHEDULED", _("Scheduled")
        COMPLETED = "COMPLETED", _("Completed")

    agreement = models.OneToOneField(
        StorageAgreement,
        on_delete=models.CASCADE,
        related_name="site_pickup",
        verbose_name=_("agreement"),
    )
    organized_by = models.CharField(
        max_length=10,
        choices=OrganizedBy.choices,
        default=OrganizedBy.SERVICE,
        verbose_name=_("organized by"),
        help_text=_(
            "Many clients cannot deliver the asset themselves, so the service organizes the "
            "pickup (rule 4)."
        ),
    )
    is_own_site = models.BooleanField(
        default=False,
        verbose_name=_("from the firm's own site"),
        help_text=_(
            "The site is named on the agreement's origin site; this "
            "records whether it is the firm's own site or another one."
        ),
    )
    lift_out_method = models.CharField(
        max_length=15,
        choices=LiftOutMethod.choices,
        blank=True,
        default="",
        verbose_name=_("lift-out method"),
        help_text=_("How the asset is lifted out for storage."),
    )
    lift_out_date = models.DateField(
        null=True, blank=True, verbose_name=_("lift-out date")
    )
    transport_mode = models.CharField(
        max_length=20,
        choices=TransportMode.choices,
        blank=True,
        default="",
        verbose_name=_("transport mode"),
        help_text=_("How the asset is delivered to the yard."),
    )
    carrier_name = models.CharField(
        max_length=255, blank=True, verbose_name=_("carrier name")
    )
    scheduled_date = models.DateField(
        null=True, blank=True, verbose_name=_("scheduled delivery date")
    )
    completed_at = models.DateTimeField(
        null=True, blank=True, verbose_name=_("completed at")
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name=_("status"),
    )
    intake_record = models.ForeignKey(
        "intake.IntakeRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("intake record"),
        help_text=_(
            "The receipt documenting the asset at pickup; its inventory "
            "rows are the condition list and the case photos the images."
        ),
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))

    class Meta:
        verbose_name = _("site pickup")
        verbose_name_plural = _("site pickups")

    def __str__(self) -> str:
        return f"SitePickup for agreement {self.agreement_id}"


class StorageTransportService(models.Model):
    """WNT-07: a transport service offered alongside storage.

    The catalog of transport services a storage client can be quoted for
    (lift-out delivery, road transport to the yard, putting into service, ...).
    Data-driven exactly like ``StorageServiceType`` and
    ``PackageComponent`` - management maintains the rows in the admin, not
    in code - and deactivating a row (``is_active=False``) keeps it out of
    new offers without deleting the history of past ones.

    Photos and documents are NOT stored here: everything in this project
    attaches files as ``attachments.Attachment`` rows through a generic
    relation (arrival photos, approval scans, delivery protocols), so a
    transport service that needs images uses that same stack.
    """

    name = models.CharField(max_length=255, unique=True, verbose_name=_("name"))
    is_active = models.BooleanField(default=True, verbose_name=_("active"))
    sort_order = models.PositiveIntegerField(
        default=0, verbose_name=_("sort order")
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = _("storage transport service")
        verbose_name_plural = _("storage transport services")

    def __str__(self) -> str:
        return self.name



