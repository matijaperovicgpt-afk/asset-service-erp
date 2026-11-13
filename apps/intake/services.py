"""Business logic for the intake app.

Implemented:
- append_intake_amendment(record, text, user): append later-discovered
  asset condition to an intake record (SVC-07 / INV-12).
- compare_handover_with_intake(handover): surface the intake checklist
  (items, amendments, photo counts) next to the handover for the human
  comparison required by SVC-18.
- create_intake_record(case, user, ...): create a intake record; only one
  unsigned record per case at a time, mount status mandatory (SVC-06),
  visible-damage description mandatory (SVC-07).
- add_intake_item(record, user, ...) / remove_intake_item(item, user):
  manage the categorized inventory (personal items, equipment, keys,
  documents) while the record is unsigned; a signed record is final and
  later findings go through amendments (SVC-06/SVC-07/INV-12).
- sign_intake_record(record, user): staff receipt confirmation, once
  only; attributed to the case's responsible technician when one exists,
  else to the acting authorized employee (SVC-05/SVC-06); refused until
  the case carries at least one arrival photo (SVC-07), until the
  asset is fully identified (§7.2) and until the fuel gauge has been
  read or declared absent (§7.6).
- create_handover_record(case, user, ...): create a handover record;
  only one unsigned record per case at a time (SVC-18).
- update_handover_note(handover, ...): edit the discrepancies note while
  the record is unsigned; a signed record is final (SVC-18).
- sign_handover(handover, received_by_name): capture the receiving
  signature as name + timestamp, once only (SVC-18); refused until the
  worker signature, the client-approval scan and the handover photos are
  all on file.
- has_handover_photos(case): §7's "photos or video at intake and
  at handover" - the departure half of the arrival rule SVC-07
  already enforces.
- sign_handover_technician(record, user) / attach_handover_approval() /
  has_handover_approval(): the two company-side handover records
  (procedure §4.4, mirroring the SVC-05 work-order pair).
"""
from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.cases.models import CaseType
from apps.cases.locking import serialised_per_row
from apps.cases.services import serialised_per_case

from .models import HandoverRecord, IntakeAmendment, IntakeItem, IntakeRecord


def append_intake_amendment(record: IntakeRecord, text: str, user) -> IntakeAmendment:
    """
    SVC-07 / INV-12: record asset condition discovered after intake as
    an append-only amendment; the intake record itself is never edited.

    An amendment is a legal addendum to a signed record, so an empty one
    is refused rather than stored - and, like every other act on the
    intake record, it leaves a SYSTEM entry in the case log.
    """
    if not (text or "").strip():
        raise ValidationError(
            _("Describe what was noticed on the asset (SVC-07).")
        )
    amendment = IntakeAmendment.objects.create(
        record=record, text=text.strip(), author=user
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=record.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-07: amendment #%s appended to intake record #%s."
            % (amendment.pk, record.pk)
        ),
    )
    return amendment


@serialised_per_row
def confirm_no_intake_items(record: IntakeRecord, user) -> IntakeRecord:
    """
    SVC-06 (procedure §2.3): state explicitly that nothing was received
    with the asset, so an empty inventory reads as an answer rather than
    as an omission. Refused once the record is signed (the inventory is
    final) and refused while items are listed - the two contradict.
    """
    if record.signed_at is not None:
        raise ValidationError(
            _(
                "Intake record #%(id)s is signed and final; its inventory "
                "can no longer be changed (SVC-06)."
            ) % {"id": record.pk}
        )
    if record.items.exists():
        raise ValidationError(
            _(
                "Intake record #%(id)s lists items, so 'nothing received' "
                "cannot also be true - remove them first (SVC-06)."
            ) % {"id": record.pk}
        )
    record.no_items_received = True
    record.save(update_fields=["no_items_received"])
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=record.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-06: nothing was received with the asset on intake "
            "record #%s." % record.pk
        ),
    )
    return record


def compare_handover_with_intake(handover: HandoverRecord) -> dict:
    """
    SVC-18: at handover, condition, equipment and items are COMPARED
    against the intake record and the human confirms. This pure read
    surfaces the checklist for that comparison; it decides nothing.

    Uses the latest intake record of the case (a case can accumulate
    several, e.g. after transport legs). Also surfaces the work summary
    (SVC-15 log entries + used parts) so the state of the asset, the
    equipment AND the work performed are all read next to the intake
    record (procedure §4.4 "Handover record").
    """
    case = handover.case
    # The signed record is the receipt; an unsigned duplicate made later
    # is a draft and showed as an empty checklist (QA SV-M3).
    records = IntakeRecord.objects.filter(case=case).order_by("-created_at", "-pk")
    record = records.filter(signed_at__isnull=False).first() or records.first()
    items = []
    amendments = []
    if record is not None:
        items = [
            {
                "category": item.get_category_display(),
                "item_name": item.item_name,
                "quantity": item.quantity,
                "condition_note": item.condition_note,
            }
            for item in record.items.all()
        ]
        amendments = [amendment.text for amendment in record.amendments.all()]

    # Imported lazily: apps.attachments is a sibling domain app.
    from apps.attachments.services import attachments_for

    Attachment = apps.get_model("attachments", "Attachment")
    case_attachments = attachments_for(case)

    # SVC-15 work summary (SERVICE cases; empty lists otherwise).
    work_log = []
    used_parts = []
    total_hours = 0
    work_order = getattr(case, "work_order", None)
    if work_order is not None:
        for entry in work_order.work_log.all().select_related("performed_by"):
            work_log.append(
                {
                    "description": entry.description,
                    "hours": entry.hours,
                    "performed_by": entry.performed_by,
                    "created_at": entry.created_at,
                }
            )
            total_hours += entry.hours
        used_parts = [
            {"part": str(used.part), "quantity": used.quantity}
            for used in work_order.used_parts.all().select_related("part")
        ]

    return {
        "intake_record": record,
        "items": items,
        "amendments": amendments,
        "intake_photo_count": case_attachments.filter(
            stage=Attachment.Stage.INTAKE
        ).count(),
        # This handover's own photographs (SVC-07/§7). Counted off the
        # record, not the case: on a case with several handovers the
        # case-level figure was the sum of all of them, which made the
        # comparison read as documented when this delivery was not.
        "handover_photo_count": attachments_for(handover)
        .filter(stage=Attachment.Stage.HANDOVER)
        .count(),
        "discrepancies_note": handover.discrepancies_note,
        "work_log": work_log,
        "used_parts": used_parts,
        "total_hours": total_hours,
    }


def has_handover_photos(handover: HandoverRecord) -> bool:
    """SVC-07 / procedure §7: is the asset photographed at THIS handover?

    The mirror of the arrival check inside ``sign_intake_record()``. The
    procedure names the photographs "at intake and at handover" in one
    breath, so the two ends of the case are documented the same way: at
    least one HANDOVER-stage PHOTO, video welcome but not a substitute.

    They hang off the record rather than off the case, and the reason is
    the sentence in ``create_handover_record``: "a case can legitimately
    accumulate several, e.g. transport legs". A case-level check let one
    photograph taken at the first delivery stand in for every delivery
    afterwards - the second asset went back on the first asset's pictures.
    The client-approval scan (``has_handover_approval``) was already
    filed this way, and ``attachment_targets_for_case`` gathers the
    handover records, so both galleries still show them.
    """
    from apps.attachments.services import attachments_for

    Attachment = apps.get_model("attachments", "Attachment")
    return attachments_for(handover).filter(
        stage=Attachment.Stage.HANDOVER, kind=Attachment.Kind.PHOTO
    ).exists()


def has_handover_approval(handover: HandoverRecord) -> bool:
    """SVC-18: is the client-signed handover scan attached to the record?"""
    from apps.attachments.services import attachments_for

    Attachment = apps.get_model("attachments", "Attachment")
    return attachments_for(handover).filter(kind=Attachment.Kind.DOCUMENT).exists()


def attach_handover_approval(handover: HandoverRecord, file, user):
    """
    SVC-18 (procedure §4.4): the client's approval of taking the asset
    back is a scanned, signed document, stored as a DOCUMENT attachment
    on the handover record itself (INV-08). Re-uploads are allowed - the
    guard only requires that at least one scan exists.
    """
    from apps.attachments.services import attach_file

    Attachment = apps.get_model("attachments", "Attachment")
    attachment = attach_file(
        handover,
        file,
        Attachment.Kind.DOCUMENT,
        Attachment.Stage.HANDOVER,
        user=user,
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=handover.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-18: client handover-approval scan '%s' attached to "
            "handover record #%s." % (attachment.original_name, handover.pk)
        ),
    )
    return attachment


@serialised_per_row
def sign_handover_technician(handover: HandoverRecord, user) -> HandoverRecord:
    """
    SVC-18 (procedure §4.4): the responsible worker digitally signs the
    handover record in-app, approving that state, equipment and work
    done were compared against the intake record. Sign-once; refused on
    a record the client has already confirmed (final).
    """
    if user is None:
        raise ValidationError(
            _("A staff user is required to sign the handover record (SVC-18).")
        )
    # "odgovorni radnik": the worker who did the job or the one
    # responsible for it - not the front desk (QA SV-M7).
    if not user.is_superuser and user.role == "FRONT_DESK":
        raise ValidationError(
            _(
                "The handover is signed by the responsible worker - a "
                "technician or a manager (SVC-18)."
            )
        )
    if handover.signed_at is not None:
        raise ValidationError(
            _(
                "Handover record #%(id)s is already confirmed by the client "
                "and final (SVC-18)."
            ) % {"id": handover.pk}
        )
    if handover.technician_signed_at is not None:
        raise ValidationError(
            _(
                "Handover record #%(id)s was already signed by the worker "
                "at %(when)s and cannot be re-signed (SVC-18)."
            ) % {"id": handover.pk, "when": handover.technician_signed_at}
        )
    handover.signed_by_technician = user
    handover.technician_signed_at = timezone.now()
    handover.save(update_fields=["signed_by_technician", "technician_signed_at"])
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=handover.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-18: handover record #%s signed by worker '%s'."
            % (handover.pk, user.get_username())
        ),
    )
    return handover


@serialised_per_case
def create_intake_record(
    case,
    user,
    *,
    mount_status: str = "",
    cleanliness_status: str = "",
    storage_location: str = "",
    energy_level: str = "",
    damage_note: str = "",
    general_condition_note: str = "",
) -> IntakeRecord:
    """
    SVC-06: create the intake record that follows the
    asset through every later phase. On a SERVICE case the work order
    comes first in the procedure (SVC-02), so creation is refused until
    one is opened - preliminary is enough. Only one unsigned record may
    exist per case at a time; a previously SIGNED record does not block
    (a case can accumulate several, e.g. after transport legs).

    Whether the asset stays on its mount or is taken off is mandatory
    bookkeeping - an explicit choice, never a default - and so is whether
    the asset arrived clean or was washed (SVC-07): the arrival photos
    are only worth what that answer says, since a dirty outline hides the
    damage they exist to record. Storage location is required exactly
    when the asset comes OFF its mount, because then it has to be put
    somewhere and the procedure records where; on its own trailer it
    stays optional (the firm operates a single site). The fuel level is
    a gauge reading recorded only when relevant; visible damage has its
    own field, separate from general remarks.

    Inventory rows are added with ``add_intake_item()``; later condition
    findings are appended with ``append_intake_amendment()``
    (SVC-07 / INV-12).
    """
    if case.case_type == CaseType.SERVICE:
        WorkOrder = apps.get_model("service", "WorkOrder")
        if not WorkOrder.objects.filter(case=case).exists():
            raise ValidationError(
                _(
                    "Open the work order before creating the intake record "
                    "(SVC-02/SVC-06)."
                )
            )
        # SVC-03: an asset worked on where it lies is never received, so
        # there is nothing an intake record could truthfully record. Only
        # the template hid the form; a POST made and signed one (QA
        # SV-M1).
        if not case.requires_asset_intake:
            raise ValidationError(
                _(
                    "Case #%(id)s is worked where the asset lies, so no "
                    "intake record is made for it (SVC-03)."
                ) % {"id": case.pk}
            )
    if mount_status not in (
        IntakeRecord.MountStatus.ON_MOUNT,
        IntakeRecord.MountStatus.OFF_MOUNT,
    ):
        raise ValidationError(
            _(
                "Record whether the asset stays on its mount or is "
                "taken off (SVC-06)."
            )
        )
    if cleanliness_status not in IntakeRecord.CleanlinessStatus.values:
        raise ValidationError(
            _(
                "Record whether the asset arrived clean or was washed on "
                "arrival - the photos are read against it (SVC-07)."
            )
        )
    if (
        mount_status == IntakeRecord.MountStatus.OFF_MOUNT
        and not storage_location.strip()
    ):
        raise ValidationError(
            _(
                "The asset is taken off its mount, so record where it "
                "will be stored (SVC-06)."
            )
        )
    if energy_level and energy_level not in IntakeRecord.EnergyLevel.values:
        raise ValidationError(
            _("Unknown fuel level '%(level)s' (SVC-06).") % {"level": energy_level}
        )
    if not damage_note.strip():
        raise ValidationError(
            _(
                "Describe the visible damage and issues observed on arrival "
                "- write 'none' when the asset is undamaged (SVC-07)."
            )
        )
    open_record = (
        IntakeRecord.objects.filter(case=case, signed_at__isnull=True)
        .order_by("-created_at", "-pk")
        .first()
    )
    if open_record is not None:
        raise ValidationError(
            _(
                "Case #%(id)s already has an unsigned intake record "
                "(#%(record)s); sign it instead of creating another (SVC-06)."
            ) % {"id": case.pk, "record": open_record.pk}
        )
    record = IntakeRecord.objects.create(
        case=case,
        received_by=user,
        mount_status=mount_status,
        cleanliness_status=cleanliness_status,
        storage_location=storage_location,
        energy_level=energy_level,
        damage_note=damage_note,
        general_condition_note=general_condition_note,
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text="SVC-06: intake record #%s created." % record.pk,
    )
    return record


def add_intake_item(
    record: IntakeRecord,
    user,
    *,
    category: str,
    item_name: str,
    quantity: int = 1,
    condition_note: str = "",
) -> IntakeItem:
    """
    SVC-06 (procedure §3): list a personal item, piece of equipment, key
    or document on the intake record, with its condition. Only while the
    record is unsigned - a signed intake record is final and later findings
    go through ``append_intake_amendment()`` (SVC-07 / INV-12).
    """
    if record.signed_at is not None:
        raise ValidationError(
            _(
                "Intake record #%(id)s is signed and final; record "
                "later-discovered items as an amendment (SVC-07)."
            ) % {"id": record.pk}
        )
    if category not in IntakeItem.Category.values:
        raise ValidationError(
            _("Unknown item category '%(category)s' (SVC-06).")
            % {"category": category}
        )
    if not (item_name or "").strip():
        raise ValidationError(_("An item name is required (SVC-06)."))
    if quantity < 1:
        raise ValidationError(_("Item quantity must be at least 1 (SVC-06)."))
    item = IntakeItem.objects.create(
        record=record,
        category=category,
        item_name=item_name.strip(),
        quantity=quantity,
        condition_note=condition_note,
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=record.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-06: item '%s' x%s (%s) listed on intake record #%s."
            % (item.item_name, item.quantity, item.category, record.pk)
        ),
    )
    return item


def remove_intake_item(item: IntakeItem, user) -> None:
    """
    SVC-06: remove a mistakenly listed item while the intake record is
    unsigned; the signed inventory is final.
    """
    record = item.record
    if record.signed_at is not None:
        raise ValidationError(
            _(
                "Intake record #%(id)s is signed and final; its inventory "
                "can no longer be changed (SVC-06)."
            ) % {"id": record.pk}
        )
    description = "%s x%s (%s)" % (item.item_name, item.quantity, item.category)
    item.delete()
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=record.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-06: item %s removed from unsigned intake record #%s."
            % (description, record.pk)
        ),
    )


def _require_identified_asset(case) -> None:
    """
    Procedure §7.2: the intake record is a receipt for a particular asset.

    The six asset data - name, type, manufacturer, model, registration,
    length - are what say *which* asset, and a receipt that cannot say
    which asset is not a receipt. So this is asked at the signature, next
    to the arrival photos (SVC-07) and the inventory (SVC-06), which are
    the other two things the same document attests to.

    A case with no asset in the registry at all is refused here for the
    same reason. That is triage's job (GEN-03) and the case page has
    carried a triage banner for it since the beginning; what is new is
    that the banner can no longer be ignored all the way to a signed
    custody document.
    """
    asset = case.asset
    if asset is None:
        raise ValidationError(
            _(
                "Case #%(id)s has no asset record yet - match or create "
                "one at triage before signing for the asset (§7.2)."
            ) % {"id": case.pk}
        )
    missing = asset.missing_identification()
    if missing:
        raise ValidationError(
            _(
                "Asset '%(asset)s' is not fully identified - %(missing)s "
                "still missing. A receipt has to say which asset it is for "
                "(§7.2)."
            ) % {
                "asset": asset.name or asset.pk,
                "missing": ", ".join(str(m) for m in missing),
            }
        )


@serialised_per_row
def sign_intake_record(record: IntakeRecord, user) -> IntakeRecord:
    """
    SVC-05/SVC-06 (procedure §2 "Preuzimanje"): a staff signature
    confirms the asset was received, once only. The signature is
    attributed to the case's responsible technician when one is assigned
    (the technician responsible for the servicing work); otherwise it
    falls back to the acting authorized employee. A signed intake record
    is half of the ``asset_received`` guard (with the signed final work
    order for SERVICE cases).
    """
    if user is None:
        raise ValidationError(
            _("A staff user is required to sign the intake record (SVC-06).")
        )
    if record.signed_at is not None:
        raise ValidationError(
            _(
                "Intake record #%(id)s was already signed at %(when)s and "
                "cannot be re-signed (SVC-06)."
            ) % {"id": record.pk, "when": record.signed_at}
        )
    _require_identified_asset(record.case)
    # SVC-07 (procedure §4 "Fotografisanje i pranje"): the asset is
    # photographed on arrival - receipt cannot be confirmed until at
    # least one arrival photo is on the case; video stays optional.
    from apps.attachments.services import attachments_for

    Attachment = apps.get_model("attachments", "Attachment")
    has_arrival_photo = attachments_for(record.case).filter(
        stage=Attachment.Stage.INTAKE, kind=Attachment.Kind.PHOTO
    ).exists()
    if not has_arrival_photo:
        raise ValidationError(
            _(
                "Photograph the asset on arrival (wash it first if dirty) "
                "and upload at least one intake photo before confirming "
                "receipt (SVC-07)."
            )
        )
    # §7.6: the intake record lists "items, equipment, keys, fuel and
    # visible damage". Four of those five were already refused
    # unanswered - the inventory below, the damage note at creation.
    # Fuel was the one left out, and a blank gauge says only that
    # nobody looked. ``NOT_APPLICABLE`` is there for an asset with no
    # tank; it is a different statement from EMPTY.
    if not record.energy_level:
        raise ValidationError(
            _(
                "Read the fuel gauge before confirming receipt, or record "
                "that the asset has no tank (§7.6)."
            )
        )
    # SVC-06 (procedure §2.3): the inventory is part of what the
    # signature attests to, so an unanswered one cannot be signed over.
    if not record.no_items_received and not record.items.exists():
        raise ValidationError(
            _(
                "List the personal items, equipment, keys and documents "
                "received with the asset, or state explicitly that "
                "nothing was received (SVC-06)."
            )
        )
    signer = record.case.assigned_technician or user
    record.signed_by = signer
    record.signed_at = timezone.now()
    record.save(update_fields=["signed_by", "signed_at"])
    recorded_by = (
        "" if signer == user else " (recorded by '%s')" % user.get_username()
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=record.case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-06: intake record #%s signed by '%s'%s."
            % (record.pk, signer.get_username(), recorded_by)
        ),
    )
    return record


def _require_delivery_before_handover(case) -> None:
    """
    TRP-09 "After delivery ... the handover record is signed": on a
    transport case the final protocol attests to a delivery that has
    happened.

    The delivery confirmation is what says it happened, so that is what
    this asks for - not a status, which staff can move by hand. Without
    it the handover record could be created and fully client-signed while the
    truck was still on the road, and the archive would hold a signed
    receipt dated before the handover it receipts. A guard at ``closed``
    cannot fix that: by then the false timestamp is already written.
    """
    if getattr(case, "case_type", None) != CaseType.TRANSPORT:
        return
    DeliveryConfirmation = apps.get_model("transport", "DeliveryConfirmation")
    if not DeliveryConfirmation.objects.filter(
        transport_order__case=case
    ).exists():
        raise ValidationError(
            _(
                "Case #%(id)s has no delivery protocol yet - the handover "
                "record is drawn up after the asset is delivered, not "
                "before (TRP-09)."
            ) % {"id": case.pk}
        )


def _require_work_done_before_handover(case) -> None:
    """
    SVC-18: on a service case the handover record hands back an asset
    the work is finished on, so the work has to be finished.

    Same reasoning as ``_require_delivery_before_handover`` above, and
    the same shape: ask for the artefact rather than the status, because
    staff move statuses by hand. The artefact is the one
    ``_guard_closed`` already treats as proof the intervention happened -
    a final work order signed by the responsible technician.

    Without this the record could be drawn up, signed by the technician
    and signed by the client while the asset sat untouched, and the
    archive would hold a receipt for a handover that had not happened.
    Worse, it would keep holding it: ``_guard_closed`` accepts *any*
    signed handover on the case, so a record manufactured on day one
    satisfies the closing gate forever after. A guard at ``closed`` is
    too late - by then the false document is already signed and dated.
    """
    if getattr(case, "case_type", None) != CaseType.SERVICE:
        return
    WorkOrder = apps.get_model("service", "WorkOrder")
    if not WorkOrder.objects.filter(
        case=case, is_preliminary=False, signed_at__isnull=False
    ).exists():
        raise ValidationError(
            _(
                "Case #%(id)s has no work order signed by the responsible "
                "technician yet - the handover record is drawn up once the "
                "work is finished, not before (SVC-18)."
            ) % {"id": case.pk}
        )


@serialised_per_case
def create_handover_record(
    case, user, discrepancies_note: str = ""
) -> HandoverRecord:
    """
    SVC-18: create the handover record against
    which the intake checklist is compared. Only one unsigned record may
    exist per case at a time; a previously SIGNED record does not block
    (a case can legitimately accumulate several, e.g. transport legs).
    """
    _require_delivery_before_handover(case)
    _require_work_done_before_handover(case)
    open_record = (
        HandoverRecord.objects.filter(case=case, signed_at__isnull=True)
        .order_by("-created_at", "-pk")
        .first()
    )
    if open_record is not None:
        raise ValidationError(
            _(
                "Case #%(id)s already has an unsigned handover record "
                "(#%(record)s); sign it or record discrepancies on it "
                "instead of creating another (SVC-18)."
            ) % {"id": case.pk, "record": open_record.pk}
        )
    handover = HandoverRecord.objects.create(
        case=case,
        handed_over_by=user,
        discrepancies_note=discrepancies_note,
    )
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=case,
        author=user,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-18: handover record #%s created; condition, equipment and "
            "items are to be compared against the intake record."
            % handover.pk
        ),
    )
    return handover


def update_handover_note(
    handover: HandoverRecord, discrepancies_note: str
) -> HandoverRecord:
    """
    SVC-18: discrepancies found during the comparison are noted on the
    handover record. A signed record is final and cannot be edited.
    """
    if handover.signed_at is not None:
        raise ValidationError(
            _(
                "Handover record #%(id)s is signed and final; its "
                "discrepancies note can no longer be edited (SVC-18)."
            ) % {"id": handover.pk}
        )
    handover.discrepancies_note = discrepancies_note
    handover.save(update_fields=["discrepancies_note"])
    return handover


@serialised_per_row
def sign_handover(handover: HandoverRecord, received_by_name: str) -> HandoverRecord:
    """
    SVC-18: the client or an authorized person confirms receipt; the
    signature is captured as name + timestamp. A signed handover record
    is final and cannot be re-signed.

    Procedure §4.4: the confirmation is refused until BOTH company-side
    records exist - the worker's in-app signature
    (``sign_handover_technician``) and the client-signed scan
    (``attach_handover_approval``).

    Procedure §7: and until the asset has been photographed on the way
    out. The photographs are checked before either signature, because
    that is the order the work happens in - you photograph the asset, you
    compare it with the intake record, then the two signatures record what
    the comparison found.
    """
    if handover.signed_at is not None:
        raise ValidationError(
            _(
                "Handover record #%(id)s was already signed at %(when)s and "
                "cannot be re-signed (SVC-18)."
            ) % {"id": handover.pk, "when": handover.signed_at}
        )
    _require_delivery_before_handover(handover.case)
    if not has_handover_photos(handover):
        raise ValidationError(
            _(
                "Photograph the asset as it is handed over before "
                "confirming receipt - the intake photos have nothing to "
                "be compared against otherwise (SVC-07/SVC-18)."
            )
        )
    if handover.technician_signed_at is None:
        raise ValidationError(
            _(
                "Handover record #%(id)s is not signed by the responsible "
                "worker yet; the worker approves the comparison first "
                "(SVC-18)."
            ) % {"id": handover.pk}
        )
    if not has_handover_approval(handover):
        raise ValidationError(
            _(
                "Handover record #%(id)s has no client-signed approval scan "
                "attached; upload the signed document first (SVC-18)."
            ) % {"id": handover.pk}
        )
    handover.received_by_name = received_by_name
    handover.signed_at = timezone.now()
    handover.save(update_fields=["received_by_name", "signed_at"])
    ActivityLog = apps.get_model("cases", "ActivityLog")
    ActivityLog.objects.create(
        case=handover.case,
        author=None,
        kind=ActivityLog.Kind.SYSTEM,
        text=(
            "SVC-18: handover record #%s signed; receipt confirmed by "
            "'%s'." % (handover.pk, received_by_name)
        ),
    )
    return handover
