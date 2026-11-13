"""Models for the repair/service flow of a case."""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class WorkOrder(models.Model):
    """Work order attached to a service case.

    Two mandatory legal records exist side by side (SVC-05): the client's
    approval is the scanned, client-signed paper order (an ``Attachment``
    of kind DOCUMENT linked to this work order), and the in-app signature
    belongs to the technician responsible for the fix - the company-side
    record of when the work item actually started.
    """

    case = models.OneToOneField(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="work_order",
        verbose_name=_("case"),
    )
    is_preliminary = models.BooleanField(
        default=True,
        verbose_name=_("is preliminary"),
        help_text=_("Preliminary work order is signed at intake before diagnostics."),
    )
    # SVC-05 (procedure §2.1): "The employee checks the data, endorses
    # the order and forwards it to the service team" - the third act on this document, beside
    # the client's paper signature and the technician's in-app one. Set by
    # finalize_work_order(); blank on orders finalized before the field
    # existed, whose actor is recoverable from the activity log.
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="verified_work_orders",
        verbose_name=_("verified by"),
        help_text=_(
            "The employee who checked the client's entries and endorsed "
            "the order before it went to service."
        ),
    )
    verified_at = models.DateTimeField(
        _("verified at"), null=True, blank=True
    )
    signed_by_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_work_orders",
        verbose_name=_("signed by technician"),
        help_text=_(
            "Company-side signature: the technician responsible for the fix."
        ),
    )
    signed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("signed at"))
    # SVC-20 (procedure §4.5): "The work order and the service ticket are
    # closed only once completion, asset handover and financial status
    # have been recorded." The ticket's close was always guarded; the order itself
    # had no closed state at all, so work could still be logged against
    # a case that was finished and paid. Stamped by close_work_order(),
    # which transition_case() calls when the case reaches ``closed``.
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="closed_work_orders",
        verbose_name=_("closed by"),
    )
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    class Meta:
        verbose_name = _("work order")
        verbose_name_plural = _("work orders")

    def __str__(self) -> str:
        return f"WorkOrder for case {self.case_id}"


class Diagnostic(models.Model):
    """Diagnostic findings recorded against a work order.

    SVC-09 requires all four pieces (the fault, required works, parts and
    estimated completion) before the case may leave ``diagnostics``, so
    each is its own mandatory field. Rows created before this split may
    have blank fields; ``complete_for_case`` filters those out.
    """

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="diagnostics",
        verbose_name=_("work order"),
    )
    issue = models.TextField(verbose_name=_("issue"))
    servicing_work = models.TextField(verbose_name=_("servicing work"))
    # SVC-10 (procedure §3.2): free text for the parts the catalogue does
    # NOT know. Everything the catalogue does know belongs in
    # ``DiagnosticPart`` instead, where the shortcut guard can read it.
    #
    # The column is kept, not repurposed away: rows written before the
    # structured side existed hold the *whole* parts list as prose, and
    # parsing them into part references would be guessing at a document
    # somebody signed off. New rows hold only the uncatalogued remainder.
    parts_needed = models.TextField(
        verbose_name=_("parts not in the catalogue"),
        blank=True,
        help_text=_(
            "Only what the catalogue has no row for. Anything it knows is "
            "selected instead, so the system can check it."
        ),
    )
    # One estimated total for the parts listed above (EUR, OPEN-01); an
    # itemized per-part breakdown belongs to the quote, not the diagnostic.
    estimated_parts_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name=_("estimated parts price (€)"),
    )
    estimated_completion_date = models.DateField(
        verbose_name=_("estimated completion date")
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    class Meta:
        verbose_name = _("diagnostic")
        verbose_name_plural = _("diagnostics")

    @classmethod
    def complete_for_case(cls, case):
        """Diagnostics on the case's work order with every SVC-09 piece
        recorded (guards must not accept legacy partial rows).

        "Parts needed" is answered by EITHER structured rows or the
        uncatalogued free text. Requiring the text outright, as this did
        while it was the only place parts could go, would have made a
        fully-specified diagnostic - every part picked from the catalogue,
        nothing left over to describe - count as incomplete and lock every
        edge out of ``diagnostics``.

        Annotated rather than joined with ``__isnull``: a join to a
        multi-row relation would return the same diagnostic once per part.
        """
        return (
            cls.objects.filter(work_order__case=case)
            .exclude(issue="")
            .exclude(servicing_work="")
            .annotate(_needed_count=models.Count("needed_parts"))
            .exclude(parts_needed="", _needed_count=0)
            .filter(estimated_completion_date__isnull=False)
        )

    def __str__(self) -> str:
        return f"Diagnostic #{self.pk} for work order {self.work_order_id}"


class DiagnosticPart(models.Model):
    """One catalogue part a diagnostic says the job needs (SVC-10).

    Procedure §3.2 turns on whether the parts are "consumable and
    available" - on hand. While that answer lived in free text the
    shortcut guard could only check it negatively, through the part orders
    somebody had already raised. A part reference and a quantity let it
    check the claim itself.

    Deliberately independent of stock: a part the catalogue knows but does
    not have is still that part, and typing it as prose because the shelf
    is empty would throw away an identity the system already has. Whether
    the case takes the §3.2 shortcut or the §3.3 quote route is then
    computed from ``stock_qty``, not from which box somebody typed in.
    """

    diagnostic = models.ForeignKey(
        Diagnostic,
        on_delete=models.CASCADE,
        related_name="needed_parts",
        verbose_name=_("diagnostic"),
    )
    # PROTECT, like UsedPart and PartOrder: this is case history, and
    # ``delete_part`` refuses to remove a row any of them point at.
    part = models.ForeignKey(
        "parts.Part",
        on_delete=models.PROTECT,
        related_name="needed_by",
        verbose_name=_("part"),
    )
    quantity = models.PositiveIntegerField(
        default=1, verbose_name=_("quantity")
    )

    class Meta:
        verbose_name = _("needed part")
        verbose_name_plural = _("needed parts")
        constraints = [
            models.UniqueConstraint(
                fields=["diagnostic", "part"],
                name="unique_part_per_diagnostic",
            )
        ]

    def __str__(self) -> str:
        return f"{self.part} x{self.quantity}"


class WorkLogEntry(models.Model):
    """A single labour entry (description + hours) on a work order."""

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="work_log",
        verbose_name=_("work order"),
    )
    description = models.TextField(verbose_name=_("description"))
    hours = models.DecimalField(max_digits=5, decimal_places=2, verbose_name=_("hours"))
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="work_log_entries",
        verbose_name=_("performed by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    class Meta:
        verbose_name = _("work log entry")
        verbose_name_plural = _("work log entries")

    def __str__(self) -> str:
        return f"WorkLogEntry #{self.pk} ({self.hours} h)"


class UsedPart(models.Model):
    """A part consumed on a work order."""

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="used_parts",
        verbose_name=_("work order"),
    )
    part = models.ForeignKey(
        "parts.Part",
        on_delete=models.PROTECT,
        related_name="used_in",
        verbose_name=_("part"),
    )
    quantity = models.PositiveIntegerField(default=1, verbose_name=_("quantity"))

    class Meta:
        verbose_name = _("used part")
        verbose_name_plural = _("used parts")

    def __str__(self) -> str:
        return f"{self.quantity} x part {self.part_id} on work order {self.work_order_id}"


class CompletionNotice(models.Model):
    """Client completion notice (SVC-17, procedure §4.3 "Notifying the
    client").

    Structured fields - works performed, final cost, pickup/delivery slot
    and the chosen channel - from which the system composes the formatted
    message. E-mail notices are sent immediately (``sent_at`` stamped,
    ``EmailLog`` row via the notifications app); no SMS gateway exists, so
    an SMS notice keeps the drafted text for staff to send from a phone
    (``sent_at`` stays empty until then).
    """

    class HandoverMode(models.TextChoices):
        PICKUP = "PICKUP", _("Client picks the asset up")
        DELIVERY = "DELIVERY", _("Asset is delivered to the client")

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", _("E-mail")
        SMS = "SMS", _("SMS")

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="completion_notices",
        verbose_name=_("case"),
    )
    works_performed = models.TextField(verbose_name=_("works performed"))
    final_cost = models.DecimalField(
        max_digits=12, decimal_places=2, verbose_name=_("final cost (€)")
    )
    # SVC-17: the figure the client is told is checked against what the
    # work order records (``work_cost_so_far``). A discount or a goodwill
    # write-off is legitimate; an unexplained difference is not, so a
    # notice that departs from the recorded total carries its reason.
    cost_note = models.TextField(
        blank=True,
        verbose_name=_("reason the final cost differs"),
    )
    handover_mode = models.CharField(
        max_length=20,
        choices=HandoverMode.choices,
        verbose_name=_("pickup or delivery"),
    )
    scheduled_for = models.DateTimeField(verbose_name=_("scheduled for"))
    channel = models.CharField(
        max_length=10, choices=Channel.choices, verbose_name=_("channel")
    )
    message = models.TextField(blank=True, verbose_name=_("composed message"))
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name=_("sent at"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("created by"),
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name=_("created at"))

    class Meta:
        verbose_name = _("completion notice")
        verbose_name_plural = _("completion notices")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"CompletionNotice #{self.pk} ({self.channel}) for case {self.case_id}"


class TestRun(models.Model):
    """Quality-control test run performed after the repair work is done."""

    class Result(models.TextChoices):
        PASSED = "PASS", _("Pass")
        FAIL = "FAIL", _("Fail")

    class Kind(models.TextChoices):
        """The two controls the procedure names: "the asset is put into
        service or an appropriate test run is performed" (§4.2). Nothing finer is
        invented here - the business named two, so there are two."""

        FIELD_TEST = "FIELD_TEST", _("Asset checked in operating conditions")
        TEST_RUN = "TEST_RUN", _("Test run performed")

    work_order = models.ForeignKey(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="test_runs",
        verbose_name=_("work order"),
    )
    result = models.CharField(
        max_length=10, choices=Result.choices, verbose_name=_("result")
    )
    # Blank on runs recorded before the field existed: which control was
    # performed back then is not recoverable, and picking one would put a
    # statement into the quality record that nobody made. New runs must
    # answer it (``record_test_run``).
    kind = models.CharField(
        _("control performed"),
        max_length=20,
        choices=Kind.choices,
        blank=True,
    )
    # Three-state on purpose: NULL is "never asked" (a legacy run), which
    # is not the same as "the fault is still there". A PASS may not be
    # recorded without a True here - "safe and functional" and "fault
    # cleared" are two findings, and one does not imply the other.
    fault_cleared = models.BooleanField(
        _("reported fault cleared"), null=True, blank=True
    )
    notes = models.TextField(blank=True, verbose_name=_("notes"))
    performed_at = models.DateTimeField(
        default=timezone.now, verbose_name=_("performed at")
    )

    class Meta:
        verbose_name = _("test run")
        verbose_name_plural = _("test runs")

    def __str__(self) -> str:
        return f"TestRun #{self.pk} ({self.result})"


class ServicePriceItem(models.Model):
    """Official service price list (valid from 01.10.2025, EUR,
    excl. VAT), covering engine services (0/I/regular by engine
    category), the hourly labor rate, call-out fees by area, common
    flat-priced operations and engine conservation/deconservation.

    Catalog of suggested prices only - the quote amount stays a human
    decision (ACT-02): picking items pre-fills the quote, never
    enforces it. Rows are data, edited on the backoffice price-list
    page or in the admin; names are Serbian business data (the
    StorageServiceType convention), not translated UI strings.
    """

    class Category(models.TextChoices):
        SCHEDULED_SERVICE = "SCHEDULED_SERVICE", _("Scheduled service")
        LABOR = "LABOR", _("Labor rate")
        CALLOUT = "CALLOUT", _("Call-out fee")
        OPERATION = "OPERATION", _("Common operation")
        PRESERVATION = "PRESERVATION", _("Preservation / recommissioning")

    class ServiceKind(models.TextChoices):
        ZERO = "ZERO", _("0 (initial) service")
        FIRST = "FIRST", _("First service")
        REGULAR = "REGULAR", _("Regular service")

    class Unit(models.TextChoices):
        FLAT = "FLAT", _("flat")
        PER_HOUR = "PER_HOUR", _("per hour")

    category = models.CharField(
        max_length=20, choices=Category.choices, verbose_name=_("category")
    )
    name = models.CharField(max_length=255, verbose_name=_("name"))
    equipment_category = models.CharField(
        max_length=100,
        blank=True,
        verbose_name=_("engine category"),
        help_text=_("Only for scheduled service / preservation items."),
    )
    service_kind = models.CharField(
        max_length=10,
        choices=ServiceKind.choices,
        blank=True,
        verbose_name=_("service kind"),
        help_text=_("Only for engine service items."),
    )
    # A catalogue price never runs below zero. On the model rather than on
    # the pricing form so the admin passes it too; zero stays legal for a
    # line the company offers free.
    price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("price"),
    )
    unit = models.CharField(
        max_length=10,
        choices=Unit.choices,
        default=Unit.FLAT,
        verbose_name=_("unit"),
    )
    currency = models.CharField(
        max_length=3, default="EUR", verbose_name=_("currency")
    )
    is_active = models.BooleanField(default=True, verbose_name=_("active"))
    sort_order = models.PositiveIntegerField(
        default=0, verbose_name=_("sort order")
    )

    class Meta:
        ordering = ["category", "sort_order", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "name"], name="uniq_priceitem_category_name"
            )
        ]
        verbose_name = _("service price item")
        verbose_name_plural = _("service price items")

    def __str__(self) -> str:
        suffix = " / h" if self.unit == self.Unit.PER_HOUR else ""
        return f"{self.name}: {self.price} {self.currency}{suffix}"

class InspectionPoint(models.Model):
    """One point of the new-client full asset inspection (SVC-08).

    Template data (OPEN-03): the set of things the technician checks on
    the whole asset before a new client is billed. Rows are seeded and
    maintained in the admin; ``label`` is business data, ``system`` is
    the translated group enum. Deactivating a point
    (``is_active=False``) leaves it out of new inspections without
    deleting history.
    """

    class System(models.TextChoices):
        STRUCTURE = "STRUCTURE", _("Structure & body")
        ENGINE = "ENGINE", _("Engine / motor")
        DRIVETRAIN = "DRIVETRAIN", _("Drivetrain")
        STEERING = "STEERING", _("Steering & controls")
        ELECTRICAL = "ELECTRICAL", _("Electrical")
        FUEL = "FUEL", _("Fuel / power supply")
        HYDRAULICS = "HYDRAULICS", _("Hydraulics & pumps")
        SAFETY = "SAFETY", _("Safety equipment")
        ELECTRONICS = "ELECTRONICS", _("Electronics")
        INTERIOR = "INTERIOR", _("Interior")
        OTHER = "OTHER", _("Other")

    system = models.CharField(
        max_length=20, choices=System.choices, verbose_name=_("system")
    )
    label = models.CharField(max_length=255, verbose_name=_("checklist point"))
    is_active = models.BooleanField(default=True, verbose_name=_("active"))
    sort_order = models.PositiveIntegerField(default=0, verbose_name=_("sort order"))

    class Meta:
        ordering = ["sort_order", "system", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["system", "label"], name="uniq_inspectionpoint_system_label"
            )
        ]
        verbose_name = _("inspection point")
        verbose_name_plural = _("inspection points")

    def __str__(self) -> str:
        return f"{self.get_system_display()}: {self.label}"

class InspectionRecord(models.Model):
    """New-client full inspection performed on a work order (SVC-08).
    For a first-time client a detailed inspection of the whole asset and
    all relevant service points is performed *before* a final quote is
    issued. One record per work order; each active ``InspectionPoint`` is
    materialised as an ``InspectionCheckItem`` when the record is started.
    Signing is once-only and mirrors the intake-record signature: the
    responsible technician, or the authorized employee who performed it.
    """

    work_order = models.OneToOneField(
        WorkOrder,
        on_delete=models.CASCADE,
        related_name="inspection_record",
        verbose_name=_("work order"),
    )
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("performed by"),
    )
    general_note = models.TextField(_("general note"), blank=True)
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="signed_inspection_records",
        verbose_name=_("signed by"),
    )
    signed_at = models.DateTimeField(_("signed at"), null=True, blank=True,)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("inspection record")
        verbose_name_plural = _("inspection records")

    def __str__(self) -> str:
        return f"Inspection record #{self.pk} (work order {self.work_order_id})"

class InspectionCheckItem(models.Model):
    """Jedna stiklirana tacka na ispekcionom zapisu (SVC-08).
    Svaki aktivan ``InspectionPoint`` izaziva red; tehnicar postavlja
    ``status`` i opcionu belesku. Zapis moze biti potpisan tek kada svaki
    red ima status (no ``UNRESOLVED`` rows left).
    """

    class Status(models.TextChoices):
        UNRESOLVED = "", _("Not checked")
        OK = "OK", _("OK")
        ATTENTION = "ATTENTION", _("Needs attention")
        DEFECT = "DEFECT", _("Defect")
        NOT_APPLICABLE = "NA", _("Not applicable")

    record = models.ForeignKey(
        InspectionRecord,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("inspection record"),
    )
    point = models.ForeignKey(
        InspectionPoint,
        on_delete=models.PROTECT,
        related_name="check_items",
        verbose_name=_("inspection point"),
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        blank=True,
        verbose_name=_("status"),
    )
    note = models.CharField(_("note"), max_length = 500, blank=True)

    class Meta:
        ordering = ["point__sort_order", "point__system", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "point"], name="uniq_checkitem_record_point"
            )
        ]
        verbose_name = _("inspection check item")
        verbose_name_plural = _("inspection check items")
    def __str__(self) -> str:
        return f"{self.point} = {self.status or '--'}"
