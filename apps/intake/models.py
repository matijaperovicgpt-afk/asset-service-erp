"""Models for asset intake and handover records.

Business documents:
- IntakeRecord = intake record (asset intake record)
- HandoverRecord = handover record
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.cases.models import AppendOnlyModel


class IntakeRecord(models.Model):
    """Asset intake record.

    Documents the state of the asset and everything received with it
    (personal items, equipment, keys, documents, visible damage) at the
    moment the asset is taken in for servicing.
    """

    class MountStatus(models.TextChoices):
        ON_MOUNT = "ON_MOUNT", _("Stays on its stand / pallet")
        OFF_MOUNT = "OFF_MOUNT", _("Taken off its stand / pallet")

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="intake_records",
        verbose_name=_("case"),
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("received by"),
    )
    # SVC-06: mandatory at creation (enforced in the service); blank only
    # on legacy rows that predate the explicit choice.
    mount_status = models.CharField(
        _("mount status"),
        max_length=20,
        choices=MountStatus.choices,
        blank=True,
    )
    # Optional - the firm operates a single site; used for storage halls.
    storage_location = models.CharField(
        _("storage location"), max_length=255, blank=True
    )
    class EnergyLevel(models.TextChoices):
        EMPTY = "EMPTY", _("Empty")
        QUARTER = "QUARTER", _("1/4")
        HALF = "HALF", _("1/2")
        THREE_QUARTERS = "THREE_QUARTERS", _("3/4")
        FULL = "FULL", _("Full")
        #: §7.6 lists fuel beside items, equipment, keys and damage, all
        #: of which the intake record has to answer. A hand tool has no
        #: tank to read, and that is an answer - unlike a blank, which
        #: only says nobody looked. EMPTY is a gauge reading and means
        #: something entirely different: there IS a tank, and it is dry.
        NOT_APPLICABLE = "NOT_APPLICABLE", _("Not applicable")

    class CleanlinessStatus(models.TextChoices):
        CLEAN = "CLEAN", _("Clean on arrival")
        CLEANED = "CLEANED", _("Dirty - cleaned on arrival")
        NOT_CLEANED = "NOT_CLEANED", _("Dirty - not cleaned")

    # SVC-07 (procedure §2.4 "Fotografisanje i pranje"): a dirty asset is
    # washed on arrival so hidden damage can be established - which means
    # the arrival photos are only as good as the answer to this. Mandatory
    # at creation (enforced in the service); blank only on rows that
    # predate the field.
    cleanliness_status = models.CharField(
        _("cleanliness on arrival"),
        max_length=20,
        choices=CleanlinessStatus.choices,
        blank=True,
    )
    # SVC-06 (procedure §2.3): an empty item list is ambiguous - nothing
    # was received, or nobody looked. This is the explicit "nothing was
    # received with the asset" answer, and receipt cannot be confirmed
    # without either it or at least one item, exactly as damage_note
    # forces "none" rather than silence.
    no_items_received = models.BooleanField(
        _("nothing received with the asset"),
        default=False,
    )
    # SVC-06: "fuel level, where relevant" - a gauge reading. The
    # column stays permissive because a record is drafted before the
    # asset is looked over; the signature is what refuses a blank
    # (§7.6), and ``NOT_APPLICABLE`` is how "there is no tank" is said
    # out loud.
    energy_level = models.CharField(
        _("fuel level"),
        max_length=20,
        choices=EnergyLevel.choices,
        blank=True,
    )
    # Visible damage noted at intake; after signing, later findings go
    # into amendments (SVC-07 / INV-12), never over this field.
    damage_note = models.TextField(_("visible damage note"), blank=True)
    general_condition_note = models.TextField(
        _("general condition note"), blank=True
    )
    signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="signed_intake_records",
        verbose_name=_("signed by (staff)"),
        help_text=_(
            "Receipt confirmation: the responsible technician for the "
            "work item, or the authorized employee who received the asset."
        ),
    )
    signed_at = models.DateTimeField(_("signed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("intake record")
        verbose_name_plural = _("intake records")

    def __str__(self) -> str:
        return f"Intake record #{self.pk} (case {self.case_id})"


class IntakeAmendment(AppendOnlyModel):
    """Later-discovered asset condition, appended to an intake record.

    SVC-07 / INV-12: anything noticed after the intake record is signed
    is appended as a new dated entry - the original record is never
    edited over.
    """

    record = models.ForeignKey(
        IntakeRecord,
        on_delete=models.CASCADE,
        related_name="amendments",
        verbose_name=_("intake record"),
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("author"),
    )
    text = models.TextField(_("text"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("intake amendment")
        verbose_name_plural = _("intake amendments")
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"Amendment #{self.pk} to intake record {self.record_id}"


class IntakeItem(models.Model):
    """Single item listed on an intake record.

    SVC-06 (procedure §3 "Condition and equipment inventory"): the inventory is
    categorized - personal items, equipment, keys, documents - so the
    intake checklist and the SVC-18 handover comparison can be read per
    category instead of as one flat note.
    """

    class Category(models.TextChoices):
        PERSONAL_ITEM = "PERSONAL", _("Personal item")
        EQUIPMENT = "EQUIPMENT", _("Equipment")
        KEY = "KEY", _("Key")
        DOCUMENT = "DOCUMENT", _("Document")
        OTHER = "OTHER", _("Other")

    record = models.ForeignKey(
        IntakeRecord,
        on_delete=models.CASCADE,
        related_name="items",
        verbose_name=_("intake record"),
    )
    category = models.CharField(
        _("category"),
        max_length=20,
        choices=Category.choices,
        default=Category.OTHER,
    )
    item_name = models.CharField(_("item name"), max_length=255)
    quantity = models.PositiveIntegerField(_("quantity"), default=1)
    condition_note = models.CharField(
        _("condition note"), max_length=255, blank=True
    )

    class Meta:
        verbose_name = _("intake item")
        verbose_name_plural = _("intake items")
        ordering = ["category", "item_name", "pk"]

    def __str__(self) -> str:
        return f"{self.item_name} x{self.quantity}"


class HandoverRecord(models.Model):
    """Handover record.

    Created when the asset is returned to the client. Its contents are
    compared against the intake record to surface any
    discrepancies - see ``services.compare_handover_with_intake()``
    (SVC-18); the human confirms the comparison.
    """

    case = models.ForeignKey(
        "cases.Case",
        on_delete=models.CASCADE,
        related_name="handover_records",
        verbose_name=_("case"),
    )
    handed_over_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("handed over by"),
    )
    received_by_name = models.CharField(
        _("received by (name)"), max_length=255, blank=True
    )
    discrepancies_note = models.TextField(_("discrepancies note"), blank=True)
    # SVC-18 (procedure §4.4 "Handover record"): the worker's in-app
    # digital signature approving the handover - the company-side record,
    # mirroring the work-order signature (SVC-05). Required, together
    # with the client-approval scan, before the client receipt
    # confirmation below can be captured.
    signed_by_technician = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="technician_signed_handovers",
        verbose_name=_("signed by (worker)"),
        help_text=_(
            "The staff member approving the handover after comparing the "
            "asset state against the intake record."
        ),
    )
    technician_signed_at = models.DateTimeField(
        _("worker signed at"), null=True, blank=True
    )
    signed_at = models.DateTimeField(_("signed at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("handover record")
        verbose_name_plural = _("handover records")

    def __str__(self) -> str:
        return f"Handover record #{self.pk} (case {self.case_id})"
