"""Forms for the internal backoffice."""

from decimal import Decimal

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import ApprovalLimit
from apps.accounts.services import LADDER_ROLES, validate_approval_limit
from apps.cases.models import (
    CONTACT_ROLES_WITH_OWN_DETAILS,
    ActivityLog,
    CaseType,
    ContactRole,
    ReportChannel,
)
from apps.service.models import ServicePriceItem
from apps.transport.models import (
    DeliveryConfirmation,
    LoadingConfirmation,
    LoadingPlan,
)
from apps.storage.models import (
    SitePickup,
    StorageAgreement,
    StorageOrientation,
    StoragePriceBand,
    StorageZone,
)
from apps.storage.services import MAX_ASSET_LENGTH_M, validate_band

from .formmixins import BootstrapFormMixin
from .widgets import (
    FiveMinuteDateTimeWidget,
    PartCheckboxSelect,
    PriceItemCheckboxSelect,
)

#: Upper bound on any typed item/part quantity. The columns are 32-bit
#: integers; a larger number reached the database and came back as a 500.
QUANTITY_MAX = 9999


class CaseCreateForm(BootstrapFormMixin, forms.Form):
    """
    Staff intake of a report received by phone/e-mail/walk-in (GEN-01
    step 1). The portal has its own public form; PORTAL is therefore not
    offered as a channel here. The initial status is derived from the
    case type by the service - staff never pick it.
    """
    # A full-page form, not one of the dense case-detail cards.
    bootstrap_size = None

    #: The fields the template pulls out into their own storage fieldset.
    #: Named here rather than listed in the template, which used to exclude
    #: them with a chain of ``field.name != "..."`` comparisons - adding a
    #: field there silently dropped it into the generic loop, and renaming
    #: one silently rendered it twice.
    STORAGE_FIELDS = (
        "permit_no",
        "asset_length_m",
        "asset_width_m",
        "origin_site",
    )


    # PORTAL cases are created by the public form only.
    CHANNEL_CHOICES = [
        (ReportChannel.PHONE, ReportChannel.PHONE.label),
        (ReportChannel.EMAIL, ReportChannel.EMAIL.label),
        (ReportChannel.IN_PERSON, ReportChannel.IN_PERSON.label),
    ]

    case_type = forms.ChoiceField(
        label=_("Case type"),
        choices=CaseType.choices,
    )
    report_channel = forms.ChoiceField(
        label=_("Report channel"),
        choices=CHANNEL_CHOICES,
    )
    contact_name = forms.CharField(
        label=_("Contact name"),
        max_length=255,
    )
    phone = forms.CharField(
        label=_("Phone"),
        max_length=50,
        required=False,
    )
    email = forms.EmailField(
        label=_("Email"),
        required=False,
    )
    asset_info = forms.CharField(
        label=_("Asset info"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    location = forms.CharField(
        label=_("Location"),
        max_length=255,
        required=False,
    )
    symptoms_description = forms.CharField(
        label=_("Problem / request description"),
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    # SVC-01 / §7.4 - optional at creation, like the WNT-01 fields
    # below: a phone report is taken in whatever order the caller
    # speaks. The diagnostics guard enforces completeness. Ignored for
    # non-SERVICE case types, which report no fault.
    client_diagnosis = forms.CharField(
        label=_("Client's assessment of the fault"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_("What the client thinks is wrong, in their words."),
    )
    # WNT-01 inquiry documentation - optional at creation (a phone
    # inquiry may be partial); the contracted guard enforces
    # completeness. Ignored for non-STORAGE case types.
    permit_no = forms.CharField(
        label=_("Permit number"),
        max_length=100,
        required=False,
    )
    asset_length_m = forms.DecimalField(
        label=_("Asset length (m)"),
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.1"),
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )
    asset_width_m = forms.DecimalField(
        label=_("Asset width (m)"),
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.1"),
        required=False,
        widget=forms.NumberInput(attrs={"step": "0.01"}),
    )
    origin_site = forms.CharField(
        label=_("Origin site"),
        max_length=255,
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        channel = cleaned.get("report_channel")
        # §7.3: a fault report that does not say where the asset is
        # cannot be acted on. Storage and transport inquiries carry
        # their own origin fields, so this is asked of service cases.
        if cleaned.get("case_type") == CaseType.SERVICE and not (
            cleaned.get("location") or ""
        ).strip():
            self.add_error("location", _("Record where the asset is (§7.3)."))
        # The creation confirmation (NTF-04) goes out on the channel the
        # client used, so that contact detail must be captured.
        if channel == ReportChannel.PHONE and not cleaned.get("phone"):
            self.add_error("phone", _("Phone is required for a phone report."))
        if channel == ReportChannel.EMAIL and not cleaned.get("email"):
            self.add_error("email", _("Email is required for an email report."))
        if not cleaned.get("phone") and not cleaned.get("email"):
            self.add_error(
                "phone", _("At least one of phone or email is required.")
            )
        # The same sanity rules ``record_storage_entry`` enforces (QA S-M4),
        # checked here so a bad breadth cannot 500 after the case is created.
        length = cleaned.get("asset_length_m")
        width = cleaned.get("asset_width_m")
        if length is not None and length > MAX_ASSET_LENGTH_M:
            self.add_error(
                "asset_length_m",
                _("An asset length of %(len)s m is not plausible (at most %(max)s m).")
                % {"len": length, "max": MAX_ASSET_LENGTH_M},
            )
        if length is not None and width is not None and width > length:
            self.add_error(
                "asset_width_m",
                _("The breadth (%(w)s m) cannot be larger than the length (%(l)s m).")
                % {"w": width, "l": length},
            )
        return cleaned


class ActivityLogForm(BootstrapFormMixin, forms.ModelForm):
    """
    Inline activity-log entry on the case detail page (GEN-02).

    ``PHONE_AGREEMENT`` is offered so agreements made by phone that affect
    price, scope or deadline are documented after the fact; ``SYSTEM``
    entries are written by services only, so they are not offered here.
    """

    KIND_CHOICES = [
        (ActivityLog.Kind.NOTE, _("Note")),
        (ActivityLog.Kind.PHONE_AGREEMENT, _("Phone agreement")),
    ]

    kind = forms.ChoiceField(
        label=_("Kind"),
        choices=KIND_CHOICES,
    )

    class Meta:
        model = ActivityLog
        fields = ["kind", "text"]
        widgets = {
            "text": forms.Textarea(attrs={"rows": 3}),
        }


class PartOrderCreateForm(BootstrapFormMixin, forms.Form):
    """Order a part for the case (SVC-10/SVC-11).

    The part price is typed freely; for a non-stock part the mandatory
    50% advance (INV-05) is computed from that price and recorded
    automatically - staff never fill in a separate advance object.
    """

    part = forms.ModelChoiceField(
        label=_("Part"),
        queryset=None,
    )
    price = forms.DecimalField(
        label=_("Part price (€)"),
        required=False,
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(
            attrs={
                "placeholder": _("Part price (€)"),
            }
        ),
    )
    expected_delivery = forms.DateField(
        label=_("Expected delivery"),
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.parts.models import Part

        self.fields["part"].queryset = Part.objects.order_by("code")

    def clean(self):
        cleaned = super().clean()
        part = cleaned.get("part")
        if part is not None and cleaned.get("price") is None:
            from apps.parts.services import is_stock_part

            if not is_stock_part(part):
                self.add_error(
                    "price",
                    _(
                        "A price is required for a non-stock part: the 50% "
                        "advance is computed from it (INV-05)."
                    ),
                )
        return cleaned


class WorkLocationForm(BootstrapFormMixin, forms.Form):
    """SVC-03: record the outcome of the pre-arrival consult.

    The answer is a choice because it decides the shape of the rest of
    the case (whether the asset is received at all); who was consulted
    is optional and ends up in the activity log, not in a column.
    """

    work_location = forms.ChoiceField(
        label=_("Where is the work done?"),
    )
    consulted = forms.ModelChoiceField(
        label=_("Technician consulted"),
        queryset=None,
        required=False,
        empty_label=_("Not recorded"),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("What was agreed, and why"),
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounts.models import Role, User
        from apps.cases.models import WorkLocation

        self.fields["work_location"].choices = [
            ("", _("Choose where the work is done..."))
        ] + list(WorkLocation.choices)
        self.fields["consulted"].queryset = User.objects.filter(
            role=Role.TECHNICIAN, is_active=True
        ).order_by("username")


class DeliveryMethodForm(BootstrapFormMixin, forms.Form):
    """SVC-04: how the asset reaches the yard.

    The other half of what "Intake scheduled" means. A choice rather than
    free text, because the answer decides who has to be arranged - our
    driver, a carrier, or nobody at all - and free text cannot be counted.
    """

    delivery_method = forms.ChoiceField(
        label=_("How does the asset get here?"),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("What was agreed with the client"),
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.cases.models import DeliveryMethod

        self.fields["delivery_method"].choices = [
            ("", _("Choose how the asset arrives..."))
        ] + list(DeliveryMethod.choices)


class AppointmentBookForm(BootstrapFormMixin, forms.Form):
    """Book an appointment on the case (SVC-04)."""

    kind = forms.ChoiceField(
        label=_("Kind"),
    )
    scheduled_for = forms.DateTimeField(
        label=_("Scheduled for"),
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=FiveMinuteDateTimeWidget(),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("Note"),
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.scheduling.models import Appointment

        self.fields["kind"].choices = Appointment.Kind.choices


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A FileField that accepts several files in one input (Django has no
    built-in multi-upload field); every file is cleaned individually and
    the cleaned value is always a list."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            if not data:
                single_clean(None, initial)  # raises when the field is required
                return []
            return [single_clean(item, initial) for item in data]
        cleaned = single_clean(data, initial)
        return [cleaned] if cleaned else []


#: Photo extensions accepted for the mandatory arrival photos (SVC-07);
#: the video set is shared with the attachments app.
PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}


def _validate_extensions(files, allowed, message):
    bad = [
        f.name
        for f in files
        if f.name.rsplit(".", 1)[-1].lower() not in allowed
    ]
    if bad:
        raise forms.ValidationError(
            message
            % {"names": ", ".join(bad), "extensions": ", ".join(sorted(allowed))}
        )
    return files


class IntakeRecordCreateForm(BootstrapFormMixin, forms.Form):
    """Create the intake record (intake record, SVC-06) together with the
    mandatory arrival documentation (SVC-07): a visible damage/issues
    description plus at least one arrival photo; videos stay optional.
    Item rows are added on the case page (``IntakeItemForm``). The trailer
    status is an explicit mandatory choice - the service refuses a blank
    value."""

    mount_status = forms.ChoiceField(
        label=_("Trailer"),
    )
    cleanliness_status = forms.ChoiceField(
        label=_("Condition on arrival"),
    )
    storage_location = forms.CharField(
        label=_("Storage location"),
        required=False,
        help_text=_("Required when the asset comes off its mount."),
        widget=forms.TextInput(
            attrs={
                "placeholder": _("Storage location"),
            }
        ),
    )
    # §7.6: the intake record lists fuel beside items, keys and damage, so
    # the gauge is read rather than skipped. "No fuel tank" is the
    # answer for an asset that has none - and it says something the blank
    # never did.
    energy_level = forms.ChoiceField(
        label=_("Fuel level"),
    )
    damage_note = forms.CharField(
        label=_("Visible damage / issues on arrival"),
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _(
                    "Describe every visible damage, scratch or issue "
                    "observed on arrival; write 'none' if the asset is "
                    "undamaged"
                ),
            }
        ),
    )
    general_condition_note = forms.CharField(
        label=_("General condition"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Other remarks"),
            }
        ),
    )
    photos = MultipleFileField(
        label=_("Arrival photos"),
        widget=MultipleFileInput(
            attrs={
                "accept": "image/jpeg,image/png,image/gif",
            }
        ),
    )
    videos = MultipleFileField(
        label=_("Arrival videos (optional)"),
        required=False,
        widget=MultipleFileInput(
            attrs={
                "accept": "video/mp4,video/quicktime",
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.intake.models import IntakeRecord

        self.fields["mount_status"].choices = [
            ("", _("Mount status..."))
        ] + list(IntakeRecord.MountStatus.choices)
        self.fields["cleanliness_status"].choices = [
            ("", _("Clean, washed, or dirty..."))
        ] + list(IntakeRecord.CleanlinessStatus.choices)
        self.fields["energy_level"].choices = [
            ("", _("Read the fuel gauge..."))
        ] + list(IntakeRecord.EnergyLevel.choices)

    def clean(self):
        """Mirror the service's off-trailer rule here, so the message
        lands on the field that caused it rather than in a flash."""
        cleaned = super().clean()
        from apps.intake.models import IntakeRecord

        if cleaned.get("mount_status") == IntakeRecord.MountStatus.OFF_MOUNT:
            if not (cleaned.get("storage_location") or "").strip():
                self.add_error(
                    "storage_location",
                    _("Record where the asset will be stored."),
                )
        return cleaned

    def clean_photos(self):
        return _validate_extensions(
            self.cleaned_data["photos"],
            PHOTO_EXTENSIONS,
            _("Not a photo: %(names)s (allowed: %(extensions)s)."),
        )

    def clean_videos(self):
        from apps.attachments.models import VIDEO_EXTENSIONS

        return _validate_extensions(
            self.cleaned_data["videos"],
            VIDEO_EXTENSIONS,
            _("Not a video: %(names)s (allowed: %(extensions)s)."),
        )


class IntakeAmendmentForm(BootstrapFormMixin, forms.Form):
    """SVC-07 / INV-12: append a later-discovered condition to a signed
    intake record. The record itself is never edited over."""

    text = forms.CharField(
        label=_("Later-discovered condition"),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _(
                    "What was noticed, and when - appended to the signed "
                    "intake record, never written over it"
                ),
            }
        ),
    )


class IntakeItemForm(BootstrapFormMixin, forms.Form):
    """List one inventory item on an unsigned intake record (SVC-06)."""

    category = forms.ChoiceField(
        label=_("Category"),
    )
    item_name = forms.CharField(
        label=_("Item"),
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("Item"),
            }
        ),
    )
    quantity = forms.IntegerField(
        label=_("Qty"),
        min_value=1,
        # A ceiling the column can hold: 99999999999 was a 500 (QA SV-M9).
        max_value=QUANTITY_MAX,
        initial=1,
        widget=forms.NumberInput(
            attrs={"class": "qty-input"}
        ),
    )
    condition_note = forms.CharField(
        label=_("Condition"),
        required=False,
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("Condition (optional)"),
            }
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.intake.models import IntakeItem

        self.fields["category"].choices = IntakeItem.Category.choices

class InspectionResultsForm(BootstrapFormMixin, forms.Form):
    """Every point of one inspection record in a single form (SVC-08).

    The checklist used to render one form per row, each with its own Save
    button, and a first-time client's inspection runs to dozens of points
    - so recording it meant dozens of round trips, each of which rebuilt
    the page. This form carries the whole list: a ``status_<pk>`` and a
    ``note_<pk>`` field per open ``InspectionCheckItem``, posted together
    and applied by :func:`apps.service.services.record_inspection_results`.
    Rows the technician did not touch are left exactly as they were.

    ``items`` is the record's checklist; it is a keyword argument so the
    detail view's ``_form`` slot can re-bind the form after a rejected
    POST the same way it re-binds every other card's form.
    """

    def __init__(self, *args, items=(), **kwargs):
        from apps.service.models import InspectionCheckItem

        super().__init__(*args, **kwargs)
        self.items = list(items)
        for item in self.items:
            self.fields[f"status_{item.pk}"] = forms.ChoiceField(
                label=_("Status"),
                required=False,  # "" = Not checked is a valid, selectable state
                choices=InspectionCheckItem.Status.choices,
                initial=item.status,
            )
            self.fields[f"note_{item.pk}"] = forms.CharField(
                label=_("Note"),
                required=False,
                max_length=500,
                initial=item.note,
                widget=forms.TextInput(attrs={"placeholder": _("Note (optional)")}),
            )
            # What the row said when the page was loaded. Every row posts
            # its pre-filled value, so "untouched" can only be told from
            # "changed" against this - comparing with the database instead
            # reverted a row another technician had changed meanwhile
            # (QA P1).
            self.fields[f"orig_status_{item.pk}"] = forms.CharField(
                required=False, initial=item.status, widget=forms.HiddenInput
            )
            self.fields[f"orig_note_{item.pk}"] = forms.CharField(
                required=False, initial=item.note, widget=forms.HiddenInput
            )
        # The mixin classed the (empty) field set in its __init__; the rows
        # were added after, so run it again over the real one.
        self.apply_bootstrap_classes()

    def rows(self):
        """``(item, status_field, note_field)`` per checklist point, in
        checklist order - what the template iterates."""
        for item in self.items:
            yield item, self[f"status_{item.pk}"], self[f"note_{item.pk}"]

    def results(self):
        """``{item_pk: (status, note)}`` for the rows the user changed on
        the page. A post without the page-load values (an older page)
        counts every row as changed, which is what it used to mean."""
        results = {}
        for item in self.items:
            status = self.cleaned_data[f"status_{item.pk}"]
            note = self.cleaned_data[f"note_{item.pk}"]
            if f"orig_status_{item.pk}" in self.data:
                original = (
                    self.cleaned_data[f"orig_status_{item.pk}"],
                    (self.cleaned_data[f"orig_note_{item.pk}"] or "").strip(),
                )
                if (status, (note or "").strip()) == original:
                    continue
            results[item.pk] = (status, note)
        return results


class WorkOrderApprovalUploadForm(BootstrapFormMixin, forms.Form):
    """Upload the client-signed work-order scan (SVC-05); the shared
    upload rules (extensions, size) live on the Attachment model."""

    file = forms.FileField(
        label=_("Scanned document"),
    )


class WorkLogEntryForm(BootstrapFormMixin, forms.Form):
    """One performed-work entry on the work order (SVC-15): what was
    done and the time spent, each its own field."""

    description = forms.CharField(
        label=_("Work done"),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("What was done"),
            }
        ),
    )
    hours = forms.DecimalField(
        label=_("Working time (h)"),
        max_digits=5,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(
            attrs={
                "step": "0.01",
                "placeholder": _("Hours"),
            }
        ),
    )


class UsedPartForm(BootstrapFormMixin, forms.Form):
    """One installed part on the work order (SVC-15)."""

    part = forms.ModelChoiceField(
        label=_("Part implemented"),
        queryset=None,
    )
    quantity = forms.IntegerField(
        label=_("Qty"),
        min_value=1,
        # A ceiling the column can hold: 99999999999 was a 500 (QA SV-M9).
        max_value=QUANTITY_MAX,
        initial=1,
        widget=forms.NumberInput(
            attrs={"class": "qty-input"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.parts.models import Part

        self.fields["part"].queryset = Part.objects.order_by("code")


class WorkDocsUploadForm(BootstrapFormMixin, forms.Form):
    """Optional photos/documents of the executed work or installed parts
    (SVC-15); stored as WORK-stage attachments, the kind derived from
    each file's extension."""

    files = MultipleFileField(
        label=_("Photos / documents of the work"),
        widget=MultipleFileInput(),
    )


class TestRunForm(BootstrapFormMixin, forms.Form):
    """Quality-control record (SVC-16, procedure §4.2): which control was
    performed, whether the reported fault is cleared, and the approval -
    PASS approves, FAIL sends the case back to rework."""

    kind = forms.ChoiceField(
        label=_("Control performed"),
    )
    result = forms.ChoiceField(
        label=_("Approval"),
    )
    fault_cleared = forms.BooleanField(
        label=_("The reported fault is cleared"),
        required=False,
        help_text=_(
            "An asset can run perfectly and still not be fixed - this is "
            "the separate finding, and a pass requires it."
        ),
    )
    notes = forms.CharField(
        label=_("Comment"),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _(
                    "What was tested and how the asset behaved "
                    "(field test / test run)"
                ),
            }
        ),
    )

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.service.models import TestRun

        self.fields["result"].choices = [("", _("Approval..."))] + list(
            TestRun.Result.choices
        )
        # SVC-03: an asset worked on on site or in the field was
        # never lifted out, so a field test is not one of its options. The
        # service refuses it either way; not offering it is the half the
        # user sees.
        kinds = list(TestRun.Kind.choices)
        if case is not None and case.works_on_site:
            kinds = [
                choice for choice in kinds if choice[0] != TestRun.Kind.FIELD_TEST
            ]
        self.fields["kind"].choices = [("", _("Control..."))] + kinds


class CompletionNoticeForm(BootstrapFormMixin, forms.Form):
    """Client completion notice (SVC-17): structured fields from which
    the system composes the message; the channel is an explicit choice."""

    works_performed = forms.CharField(
        label=_("Works performed (for the client)"),
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _("Summary of the works performed"),
            }
        ),
    )
    final_cost = forms.DecimalField(
        label=_("Final cost (€)"),
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(
            attrs={"step": "0.01"}
        ),
        help_text=_(
            "Prefilled from what the work order records: logged hours at "
            "the price list's labour rate, plus the installed parts."
        ),
    )
    cost_note = forms.CharField(
        label=_("Reason the final cost differs"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _(
                    "Only needed if the figure is not what the work order "
                    "records - a discount, a goodwill write-off, a price "
                    "agreed with the client"
                ),
            }
        ),
    )
    handover_mode = forms.ChoiceField(
        label=_("Pickup or delivery"),
    )
    scheduled_for = forms.DateTimeField(
        label=_("Scheduled for"),
        input_formats=["%Y-%m-%dT%H:%M"],
        widget=FiveMinuteDateTimeWidget(),
    )
    channel = forms.ChoiceField(
        label=_("Send as"),
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.service.models import CompletionNotice

        self.fields["handover_mode"].choices = [
            ("", _("Pickup or delivery..."))
        ] + list(CompletionNotice.HandoverMode.choices)
        self.fields["channel"].choices = CompletionNotice.Channel.choices


class WorkOrderReopenForm(BootstrapFormMixin, forms.Form):
    """Reopen a closed work order to record a correction (SVC-20)."""

    reason = forms.CharField(
        label=_("Reason for reopening"),
        widget=forms.TextInput(
            attrs={
                "placeholder": _("Why the closed order has to be changed"),
            }
        ),
    )


class AssetIdentityForm(BootstrapFormMixin, forms.ModelForm):
    """Procedure §7.2: name, type, manufacturer, model, registration,
    length.

    Until now the backoffice could only *view* an asset; the fields
    triage left blank were fixable in the Django admin and nowhere else.
    Every field is required here except the registration pair, where the
    number and the "not registered" tick answer each other - the model
    clean below is what enforces that, so the message lands on the
    control that caused it.
    """

    class Meta:
        from apps.clients.models import Asset as _Asset

        model = _Asset
        fields = (
            "name",
            "asset_type",
            "manufacturer",
            "model",
            "registration_no",
            "is_unregistered",
            "length_m",
        )
        labels = {
            "is_unregistered": _("This asset is not registered"),
        }
        help_texts = {
            "manufacturer": _("The builder of the outline, not the engine."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("name", "asset_type", "manufacturer", "model", "length_m"):
            self.fields[name].required = True
        self.fields["registration_no"].required = False

    def clean(self):
        cleaned = super().clean()
        registration = (cleaned.get("registration_no") or "").strip()
        unregistered = cleaned.get("is_unregistered")
        if registration and unregistered:
            self.add_error(
                "is_unregistered",
                _("Untick this, or clear the registration number - not both."),
            )
        elif not registration and not unregistered:
            self.add_error(
                "registration_no",
                _("Enter the registration, or tick that there is none."),
            )
        return cleaned


class ContactPersonForm(BootstrapFormMixin, forms.Form):
    """§7.1: who we ring about this case.

    The role is what is really being recorded; the three text fields are
    only reached by the two answers that point at a person the case has
    no other record of. ``clean()`` mirrors
    ``cases.services.record_contact_person`` so the message lands on a
    field instead of arriving as a page-level error - the service stays
    the authority, and both are exercised by the tests.
    """

    role = forms.ChoiceField(
        label=_("Who do we contact about this case?"),
        choices=ContactRole.choices,
    )
    name = forms.CharField(
        label=_("Contact name"),
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"placeholder": _("Only for a representative or someone else")}
        ),
    )
    phone = forms.CharField(label=_("Contact phone"), max_length=50, required=False)
    email = forms.EmailField(label=_("Contact email"), required=False)

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        if role not in CONTACT_ROLES_WITH_OWN_DETAILS:
            return cleaned
        if not (cleaned.get("name") or "").strip():
            self.add_error(
                "name", _("Name the person we should contact (§7.1).")
            )
        if not ((cleaned.get("phone") or "").strip() or (cleaned.get("email") or "").strip()):
            self.add_error(
                "phone",
                _(
                    "Give the contact a phone number or an e-mail address - "
                    "a contact person nobody can reach is not one (§7.1)."
                ),
            )
        return cleaned


class ReportLocationForm(BootstrapFormMixin, forms.Form):
    """§7.3: where the asset is. An asset moves, and a report taken as
    one site that turns out to be a private yard changes who drives
    where - so this stays editable and the change is logged."""

    location = forms.CharField(
        label=_("Where the asset is"),
        max_length=255,
        widget=forms.TextInput(
            attrs={"placeholder": _("Site or address")}
        ),
    )


class ClientAssessmentForm(BootstrapFormMixin, forms.Form):
    """SVC-01 / §7.4: what the client thinks is wrong, or that they had
    no view. Exactly one of the two - the service refuses both and
    refuses neither, and the message lands on the field here."""

    client_diagnosis = forms.CharField(
        label=_("Client's assessment of the fault"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _(
                    "What the client thinks is wrong, in their words"
                ),
            }
        ),
    )
    gave_no_assessment = forms.BooleanField(
        label=_("The client had no view of what is wrong"),
        required=False,
    )

    def clean(self):
        cleaned = super().clean()
        text = (cleaned.get("client_diagnosis") or "").strip()
        none_given = cleaned.get("gave_no_assessment")
        if text and none_given:
            self.add_error(
                "gave_no_assessment",
                _("Untick this, or clear the text - not both."),
            )
        elif not text and not none_given:
            self.add_error(
                "client_diagnosis",
                _("Write the client's assessment, or tick that they had none."),
            )
        return cleaned


class HandoverMediaUploadForm(BootstrapFormMixin, forms.Form):
    """§7/SVC-07 at the other end of the case: photograph the asset as
    it goes back out. Same shape as the arrival upload on the intake
    form - photos mandatory, videos welcome but never a substitute."""

    photos = MultipleFileField(
        label=_("Handover photos"),
        widget=MultipleFileInput(
            attrs={
                "accept": "image/jpeg,image/png,image/gif",
            }
        ),
    )
    videos = MultipleFileField(
        label=_("Handover videos (optional)"),
        required=False,
        widget=MultipleFileInput(
            attrs={
                "accept": "video/mp4,video/quicktime",
            }
        ),
    )

    def clean_photos(self):
        return _validate_extensions(
            self.cleaned_data["photos"],
            PHOTO_EXTENSIONS,
            _("Not a photo: %(names)s (allowed: %(extensions)s)."),
        )

    def clean_videos(self):
        from apps.attachments.models import VIDEO_EXTENSIONS

        return _validate_extensions(
            self.cleaned_data["videos"],
            VIDEO_EXTENSIONS,
            _("Not a video: %(names)s (allowed: %(extensions)s)."),
        )


class HandoverApprovalUploadForm(BootstrapFormMixin, forms.Form):
    """Upload the client-signed handover approval scan (SVC-18)."""

    file = forms.FileField(
        label=_("Signed handover document"),
    )


class AttachmentUploadForm(BootstrapFormMixin, forms.Form):
    """Staff upload on the case (INV-08); the shared rules live on the
    Attachment model (extensions, 20 MB) and are re-run by the service."""

    file = forms.FileField(
        label=_("File"),
    )
    kind = forms.ChoiceField(
        label=_("Kind"),
    )
    stage = forms.ChoiceField(
        label=_("Stage"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.attachments.models import Attachment

        self.fields["kind"].choices = Attachment.Kind.choices
        self.fields["stage"].choices = Attachment.Stage.choices


class DiagnosticCreateForm(BootstrapFormMixin, forms.Form):
    """Record the diagnostic on the work order (SVC-09); all four pieces
    are mandatory - the same rule the diagnostics-exit guard enforces."""

    issue = forms.CharField(
        label=_("Issue"),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Issue"),
            }
        ),
    )
    servicing_work = forms.CharField(
        label=_("Servicing work"),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Servicing work"),
            }
        ),
    )
    parts_needed = forms.CharField(
        label=_("Parts the catalogue does not have"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _(
                    "Only what could not be selected above - these have to "
                    "be ordered, and cannot be priced until they are in the "
                    "catalogue"
                ),
            }
        ),
    )
    estimated_parts_price = forms.DecimalField(
        label=_("Estimated parts price (€)"),
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0"),
        widget=forms.NumberInput(
            attrs={
                "placeholder": _("Estimated parts price (€)"),
            }
        ),
    )
    estimated_completion_date = forms.DateField(
        label=_("Estimated completion date"),
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.parts.models import Part

        # The WHOLE catalogue, not only what is on the shelf. A part the
        # catalogue knows but does not stock is still that part, and
        # retyping it as prose because the shelf is empty throws away an
        # identity the system already holds. Which route the case takes
        # (§3.2 shortcut or §3.3 quote) is computed from stock_qty by the
        # guard, not from which input box somebody typed into.
        parts = list(Part.objects.order_by("code"))
        self.fields["needed_parts"] = forms.ModelMultipleChoiceField(
            label=_("Parts needed (from the catalogue)"),
            queryset=Part.objects.all(),
            required=False,
            widget=PartCheckboxSelect(),
            help_text=_(
                "Tick what the job needs and set the quantity. Stock is "
                "shown per part; an out-of-stock part is still selected "
                "here, it just sends the case down the quote path."
            ),
        )
        for part in parts:
            self.fields[self.quantity_field_name(part)] = forms.IntegerField(
                label=_("Quantity"),
                required=False,
                min_value=1,
                max_value=QUANTITY_MAX,
                initial=1,
                widget=forms.NumberInput(
                    attrs={
                        "class": "part-qty",
                        "min": "1",
                        "step": "1",
                        "aria-label": _("Quantity"),
                    }
                ),
            )
        # BoundFields only exist once every field is registered.
        self.fields["needed_parts"].widget.quantity_fields = {
            part.pk: self[self.quantity_field_name(part)] for part in parts
        }

    @staticmethod
    def quantity_field_name(part):
        return "qty_%s" % part.pk

    def needed_part_rows(self):
        """``[(part, quantity)]`` for the ticked parts (SVC-10)."""
        return [
            (part, self.cleaned_data.get(self.quantity_field_name(part)) or 1)
            for part in self.cleaned_data.get("needed_parts") or []
        ]

    def clean(self):
        cleaned = super().clean()
        # SVC-09 still wants the parts question answered; SVC-10 only
        # changed where the answer may live. Refusing an empty text box
        # while parts are ticked would make a fully-specified diagnosis
        # look incomplete.
        if not (cleaned.get("parts_needed") or "").strip() and not cleaned.get(
            "needed_parts"
        ):
            self.add_error(
                "parts_needed",
                _(
                    "Select the parts from the catalogue, or describe the "
                    "ones it has no row for."
                ),
            )
        return cleaned


class PartQuickAddForm(BootstrapFormMixin, forms.Form):
    """Add a catalogue row from the diagnostic card (SVC-10).

    The free-text half of "parts needed" is a queue, not a resting place:
    a part with no catalogue row cannot be ordered (``PartOrder.part`` is
    a foreign key), cannot be recorded as installed (``UsedPart.part``),
    and cannot be priced - so it can never travel §3.3 -> §4.1 -> §4.3.
    This is the one-click way out of that, and the reason the catalogue
    fills itself as a by-product of ordinary work instead of waiting for
    somebody to sit down and do data entry.
    """

    code = forms.CharField(label=_("Code"), max_length=100)
    name = forms.CharField(label=_("Name"), max_length=255)
    is_consumable = forms.BooleanField(
        label=_("Regular consumable"),
        required=False,
        help_text=_("Consumables in stock are what allow the direct route."),
    )
    # Not required: only the catalogue owners see it (QA SV-H4).
    stock_qty = forms.IntegerField(
        label=_("Stock quantity"),
        min_value=0,
        max_value=QUANTITY_MAX,
        initial=0,
        required=False,
    )
    unit_price = forms.DecimalField(
        label=_("Unit price (€)"),
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0"),
        required=False,
        help_text=_(
            "Optional here, but the work cannot be totalled without it "
            "(SVC-14)."
        ),
    )

    def clean_code(self):
        from apps.parts.models import Part

        code = self.cleaned_data["code"].strip()
        if Part.objects.filter(code__iexact=code).exists():
            raise forms.ValidationError(
                _("A part with code '%(code)s' is already in the catalogue.")
                % {"code": code}
            )
        return code


class QuoteCreateForm(BootstrapFormMixin, forms.Form):
    """Draft a quote on the case (SVC-11/SVC-12).

    With ``with_catalog=True`` (SERVICE cases) staff may pick items from
    the official service price list: selected items pre-fill the amount
    (their sum) and the description, but a typed amount always wins -
    the price list only suggests, pricing stays a staff decision
    (ACT-02).
    """

    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Description"),
            }
        ),
    )
    #: The floor ``create_quote`` enforces, named once so the field and the
    #: catalogue total below cannot drift apart.
    MIN_AMOUNT = Decimal("0.01")
    #: A typing-accident ceiling: 9,999,999,999.99 € was accepted and
    #: printed in a client-facing offer (QA S-M2).
    MAX_AMOUNT = Decimal("1000000")

    amount = forms.DecimalField(
        label=_("Amount (€)"),
        max_digits=12,
        decimal_places=2,
        min_value=MIN_AMOUNT,
        max_value=MAX_AMOUNT,
        widget=forms.NumberInput(
            attrs={
                "placeholder": _("Amount (€)"),
            }
        ),
    )
    valid_until = forms.DateField(
        label=_("Valid until"),
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )

    def clean_valid_until(self):
        """A quote cannot be born expired.

        An offer whose ``valid_until`` has already passed is one the
        client cannot honour, so a back-dated one would be drafted, sent
        through whichever channel staff use, and only then found to be
        dead on arrival. Catching it here puts the complaint where it can
        still be acted on - beside the field that caused it.
        """
        valid_until = self.cleaned_data.get("valid_until")
        if valid_until is not None and valid_until < timezone.localdate():
            raise forms.ValidationError(
                _(
                    "That date has passed, so the client could not accept "
                    "the quote. Give it a date from today onwards."
                )
            )
        return valid_until

    def __init__(self, *args, with_catalog=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.with_catalog = with_catalog
        if not with_catalog:
            return
        self.fields["catalog_items"] = forms.ModelMultipleChoiceField(
            label=_("Price-list items"),
            queryset=ServicePriceItem.objects.filter(is_active=True),
            required=False,
            widget=PriceItemCheckboxSelect(),
            help_text=_(
                "Optional: tick one or more items to pre-fill the amount "
                "and description; both stay editable (the price list only "
                "suggests)."
            ),
        )
        self.fields["amount"].required = False
        self.fields["description"].required = False
        # An hourly item is a rate, not a price: summing it raw quoted an
        # unstated single hour. Each PER_HOUR item therefore gets its own
        # quantity input, rendered beside its checkbox by the widget.
        hourly = [
            item
            for item in self.fields["catalog_items"].queryset
            if item.unit == ServicePriceItem.Unit.PER_HOUR
        ]
        for item in hourly:
            self.fields[self.hours_field_name(item)] = forms.DecimalField(
                label=_("Hours"),
                required=False,
                min_value=Decimal("0.25"),
                max_digits=6,
                decimal_places=2,
                initial=Decimal("1"),
                widget=forms.NumberInput(
                    attrs={
                        "class": "price-item-hours",
                        "step": "0.25",
                        "min": "0.25",
                        "aria-label": _("Hours"),
                    }
                ),
            )
        # BoundFields only exist once every field is registered.
        self.fields["catalog_items"].widget.hours_fields = {
            item.pk: self[self.hours_field_name(item)] for item in hourly
        }

    @staticmethod
    def hours_field_name(item):
        return "hours_%s" % item.pk

    def catalog_hours(self, item):
        """Quantity for one ticked item: the staff-entered hours for a
        PER_HOUR item (one hour when left blank), one for a flat item."""
        if item.unit != ServicePriceItem.Unit.PER_HOUR:
            return Decimal("1")
        hours = self.cleaned_data.get(self.hours_field_name(item))
        return Decimal("1") if hours is None else hours

    def catalog_line(self, item):
        """``(line total, description line)`` for one ticked item."""
        if item.unit == ServicePriceItem.Unit.PER_HOUR:
            hours = self.catalog_hours(item)
            return (
                (item.price * hours).quantize(Decimal("0.01")),
                "%s — %s h × %s €/h" % (item.name, hours, item.price),
            )
        return item.price, "%s — %s €" % (item.name, item.price)

    def suggested_total(self, items=None):
        """What the price list suggests for the amount (ACT-02).

        The one definition of the sum: the quote-create view reuses it
        for the audit line, so the log can never contradict the quote.
        """
        if items is None:
            items = self.cleaned_data.get("catalog_items") or []
        return sum(
            (self.catalog_line(item)[0] for item in items), Decimal("0")
        )

    def clean(self):
        cleaned = super().clean()
        if not self.with_catalog:
            return cleaned
        items = cleaned.get("catalog_items")
        if items and cleaned.get("amount") is None:
            # The amount field's own min_value never sees this number: it
            # is assigned after field validation has run. Re-apply the
            # floor here, and complain about the items rather than about
            # the amount box - which the staff member left empty on
            # purpose, and which telling them to fill in explains nothing.
            total = self.suggested_total(items)
            if total < self.MIN_AMOUNT:
                self.add_error(
                    "catalog_items",
                    _(
                        "The ticked items total %(total)s EUR, which is not "
                        "an amount a quote can be sent for. Check the price "
                        "list: %(items)s."
                    ) % {
                        "total": total,
                        "items": ", ".join(
                            "%s (%s EUR)" % (item.name, item.price)
                            for item in items
                        ),
                    },
                )
            else:
                cleaned["amount"] = total
        if items and not (cleaned.get("description") or "").strip():
            cleaned["description"] = "\n".join(
                self.catalog_line(item)[1] for item in items
            )
        if (
            cleaned.get("amount") is None
            and "amount" not in self.errors
            # A total that fell through the floor has already been
            # explained on the items; "enter an amount" on top of it would
            # send the reader to the wrong control.
            and "catalog_items" not in self.errors
        ):
            self.add_error(
                "amount", _("Enter an amount or select price-list items.")
            )
        if not (cleaned.get("description") or "").strip() and (
            "description" not in self.errors
        ):
            self.add_error(
                "description",
                _("Enter a description or select price-list items."),
            )
        return cleaned


class StorageOfferForm(QuoteCreateForm):
    """WNT-03: the offer card on a storage case - a quote for the
    standalone storage service or for a package arrangement. Inherits
    description/amount/valid_until from ``QuoteCreateForm``; the
    commercial period defaults to the current season (WNT-04)."""

    OFFER_TYPE_CHOICES = [
        ("package", _("Package arrangement")),
        ("standalone", _("Standalone storage")),
    ]

    offer_type = forms.ChoiceField(
        label=_("Offer type"),
        choices=OFFER_TYPE_CHOICES,
        initial="package",
        widget=forms.RadioSelect,
    )
    package = forms.ModelChoiceField(
        label=_("Package"),
        queryset=None,
        required=False,
    )
    period_start = forms.DateField(
        label=_("Commercial period from"),
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )
    period_end = forms.DateField(
        label=_("Commercial period to"),
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.storage.models import StoragePackage

        self.fields["package"].queryset = StoragePackage.objects.all()
        self.fields["package"].widget.attrs.update(
            {}
        )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("offer_type") == "package" and not cleaned.get("package"):
            self.add_error(
                "package", _("A package offer needs a package (WNT-03).")
            )
        from django.core.exceptions import ValidationError as DjangoValidationError

        from apps.storage.services import validate_offer_period

        try:
            validate_offer_period(
                cleaned.get("period_start"), cleaned.get("period_end")
            )
        except DjangoValidationError as exc:
            self.add_error("period_end", exc)
        return cleaned


class StorageAgreementForm(BootstrapFormMixin, forms.ModelForm):
    """WNT-01: the inquiry-documentation card on the case page - permit,
    dimensions and the agreed price. The wider WNT-02 data (client,
    asset, ownership, site, service) lives on the dedicated
    "database entry" page (``StorageIntakeView``); the package is chosen
    only on the WNT-03 offer card so one package follows the case."""

    class Meta:
        model = StorageAgreement
        fields = [
            "permit_no",
            "asset_length_m",
            "asset_width_m",
            "accommodation",
            "term",
            "agreed_price",
        ]
        widgets = {
            "asset_length_m": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
            "asset_width_m": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
            "agreed_price": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
        }


class StoragePositionAssignForm(BootstrapFormMixin, forms.Form):
    """WNT-06: assign a hall/row/slot placement on the storage agreement."""

    hall = forms.CharField(
        label=_("Hall"),
        max_length=50,
    )
    row = forms.CharField(
        label=_("Row"),
        max_length=10,
    )
    slot = forms.CharField(
        label=_("Slot"),
        max_length=10,
    )
    planned_exit_date = forms.DateField(
        label=_("Planned exit date"),
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )


class SitePickupForm(BootstrapFormMixin, forms.ModelForm):
    """WNT rule 4: the site-pickup card on the case page - who organizes
    the pickup, the lift-out, the road delivery and its status. The site is named on the agreement's
    origin site (WNT-02); the receipt, inventory and photos are the
    shared intake stack."""

    class Meta:
        model = SitePickup
        fields = [
            "organized_by",
            "is_own_site",
            "lift_out_method",
            "lift_out_date",
            "transport_mode",
            "carrier_name",
            "scheduled_date",
            "status",
            "notes",
        ]
        widgets = {
            "lift_out_date": forms.DateInput(
                attrs={"type": "date"}
            ),
            "scheduled_date": forms.DateInput(
                attrs={"type": "date"}
            ),
            "notes": forms.Textarea(
                attrs={"rows": 2}
            ),
        }

    def clean(self):
        cleaned = super().clean()
        # QA S-M8: a delivery before the lift-out, and an external carrier
        # nobody named, were both saved.
        lift_out = cleaned.get("lift_out_date")
        delivery = cleaned.get("scheduled_date")
        if lift_out and delivery and delivery < lift_out:
            self.add_error(
                "scheduled_date",
                _("The delivery cannot come before the lift-out."),
            )
        if (
            cleaned.get("transport_mode")
            == SitePickup.TransportMode.EXTERNAL_CARRIER
            and not (cleaned.get("carrier_name") or "").strip()
        ):
            self.add_error(
                "carrier_name", _("Name the carrier doing the transport.")
            )
        return cleaned


class LoadingPlanForm(BootstrapFormMixin, forms.ModelForm):
    """TRP-02 "Loading and unloading plan": the loading/unloading-plan card on
    the case page - unloading location, lifting equipment availability, site conditions,
    the attendee at handover and the agreed loading/unloading times, filled
    in after consultation with the client."""

    class Meta:
        model = LoadingPlan
        fields = [
            "unloading_location",
            "lifting_availability",
            "lifting_notes",
            "site_conditions",
            "attendee",
            "loading_at",
            "unloading_at",
            "notes",
        ]
        widgets = {
            "lifting_notes": forms.Textarea(
                attrs={"rows": 2}
            ),
            "site_conditions": forms.Textarea(
                attrs={"rows": 2}
            ),
            # TRP-02: a carrier's loading window is the carrier's, not the
            # site's opening hours - the grid applies, the yard's hours
            # do not.
            "loading_at": FiveMinuteDateTimeWidget(working_hours=False),
            "unloading_at": FiveMinuteDateTimeWidget(working_hours=False),
            "notes": forms.Textarea(
                attrs={"rows": 2}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The five-minute widget submits "YYYY-MM-DDTHH:MM", which is
        # not in the default DATETIME_INPUT_FORMATS.
        for name in ("loading_at", "unloading_at"):
            self.fields[name].input_formats = [
                "%Y-%m-%dT%H:%M",
                "%Y-%m-%dT%H:%M:%S",
            ]

    def clean(self):
        cleaned = super().clean()
        # A plan unloading on 31.12.2025 what it loads on 01.01.2026 was
        # accepted (QA T-H1).
        loading_at = cleaned.get("loading_at")
        unloading_at = cleaned.get("unloading_at")
        if loading_at and unloading_at and unloading_at <= loading_at:
            self.add_error(
                "unloading_at",
                _("Unloading has to come after loading."),
            )
        return cleaned


class LoadingConfirmationForm(BootstrapFormMixin, forms.ModelForm):
    """TRP-07 "Loading and carriage": the internal loading-confirmation card on
    the case page - truck vs trailer and securing/road-readiness as radio
    "bars", whether a crane was used, the carrier present and an optional
    note that the client was told (outside the app). Submitting the form
    is the responsible person's sign-off (``signed_by`` / ``signed_at``
    are stamped by the service)."""

    class Meta:
        model = LoadingConfirmation
        fields = [
            "vehicle_type",
            "readiness",
            "lifting_equipment_used",
            "carrier",
            "client_notified",
            "notes",
        ]
        widgets = {
            "vehicle_type": forms.RadioSelect,
            "readiness": forms.RadioSelect,
            "notes": forms.Textarea(
                attrs={"rows": 2}
            ),
        }


class DeliveryConfirmationForm(BootstrapFormMixin, forms.ModelForm):
    """TRP-08 "Delivery": the delivery-protocol card on the case page -
    unloading organized and asset put into service as checkboxes,
    the attending owner or authorized representative (role as radio "bar",
    name mandatory - enforced by the service), the accompanying company
    employee and notes. Submitting the form is the staff member's sign-off
    (``signed_by`` / ``signed_at`` are stamped by the service)."""

    class Meta:
        model = DeliveryConfirmation
        fields = [
            "unloading_done",
            "put_into_service",
            "attendee_role",
            "attendee_name",
            "attendee_contact",
            "accompanying_employee",
            "notes",
        ]
        widgets = {
            "attendee_role": forms.RadioSelect,
            "notes": forms.Textarea(
                attrs={"rows": 2}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.contrib.auth import get_user_model

        # The accompanying employee is a company account (staff).
        self.fields["accompanying_employee"].queryset = (
            get_user_model().objects.filter(is_active=True, is_staff=True)
        )
        # TRP-08: a named attendee is mandatory on the protocol.
        self.fields["attendee_name"].required = True


class CommaDecimalField(forms.DecimalField):
    """A DecimalField that accepts the comma decimal mark people type.

    The UI runs in Serbian, where 4200,00 is how the amount is written, and
    a native number input hands the string through as typed. The view that
    used to read this value off ``request.POST`` normalised the separator
    itself; keeping that behaviour in a field means the same input works
    wherever the field is used, and a genuinely bad number still gets
    DecimalField's own error message rather than a hand-written one.
    """

    def to_python(self, value):
        if isinstance(value, str):
            value = value.replace(",", ".").strip()
        return super().to_python(value)


class TransportCarrierPriceForm(BootstrapFormMixin, forms.Form):
    """Declare the price found with an external carrier (TRP-03).

    The parsing used to live in the view, which read ``request.POST``
    directly and answered a bad number with a flash message; there was no
    form, so there was no field for the error to land on.
    """

    carrier_name = forms.CharField(
        label=_("Carrier"),
        max_length=255,
        required=False,
        help_text=_("Who is carrying it, if the offer names them."),
    )
    # The declaration is locked once made, so a 0.00 that satisfied TRP-03
    # could never be corrected (QA T-M3).
    carrier_price = CommaDecimalField(
        label=_("Carrier price (€)"),
        max_digits=8,  # matches TransportOrder.carrier_price
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.TextInput(attrs={"inputmode": "decimal"}),
    )


class HandoverSignForm(BootstrapFormMixin, forms.Form):
    """Capture the receiving signature as a name (SVC-18).

    ``received_by_name`` was a hand-written ``<input required>`` whose only
    check on an empty value was the browser's; the server refused it with a
    message quoting the rule tag.
    """

    received_by_name = forms.CharField(
        label=_("Received by (name)"),
        max_length=255,
        help_text=_("The name of the person taking delivery of the asset."),
    )
    # Optional, and only honoured when the submitting form actually carries
    # it: the sign form does not, and an absent field must not erase a note
    # saved earlier through the separate "save note" form.
    discrepancies_note = forms.CharField(
        label=_("Discrepancies noted at handover"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class QuoteAdvanceForm(BootstrapFormMixin, forms.Form):
    """Ask the client for an advance on a sent quote, as a percentage."""

    percent = forms.IntegerField(
        label=_("Advance"),
        min_value=1,
        max_value=100,
        initial=50,
        widget=forms.NumberInput(attrs={"class": "qty-input"}),
    )


class DeliveryProtocolUploadForm(BootstrapFormMixin, forms.Form):
    """Upload the signed delivery-protocol scan (TRP-08)."""

    file = forms.FileField(
        label=_("Signed delivery protocol"),
    )


class StoragePlacementForm(BootstrapFormMixin, forms.Form):
    """WNT-05: place the case's asset on the graphic yard. The x/y metre
    coordinates are written by the click-to-place JS; the server
    re-validates via ``services.place_asset``."""

    zone = forms.ModelChoiceField(
        label=_("Zone"),
        queryset=StorageZone.objects.filter(is_active=True),
    )
    pos_x_m = forms.DecimalField(
        label=_("x (m)"),
        max_digits=6,
        decimal_places=2,
        min_value=0,
        widget=forms.NumberInput(
            attrs={"step": "0.01"}
        ),
    )
    pos_y_m = forms.DecimalField(
        label=_("y (m)"),
        max_digits=6,
        decimal_places=2,
        min_value=0,
        widget=forms.NumberInput(
            attrs={"step": "0.01"}
        ),
    )
    orientation = forms.ChoiceField(
        label=_("Orientation"),
        choices=StorageOrientation.choices,
        initial=StorageOrientation.LENGTHWISE,
    )
    planned_exit_date = forms.DateField(
        label=_("Planned exit date"),
        required=False,
        widget=forms.DateInput(
            attrs={"type": "date"}
        ),
    )


class PriceBandForm(BootstrapFormMixin, forms.ModelForm):
    """OPEN-05: one price band (accommodation + length range + a price
    per storage term) on the backoffice price-list page. An empty price
    means the combination is not offered. Currency stays EUR (OPEN-01),
    so it is not offered for editing."""

    class Meta:
        model = StoragePriceBand
        fields = [
            "accommodation",
            "length_min_m",
            "length_max_m",
            "daily_price",
            "monthly_price",
            "seasonal_price",
        ]
        widgets = {
            "length_min_m": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
            "length_max_m": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
            "daily_price": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
            "monthly_price": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
            "seasonal_price": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
        }

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("length_min_m") is not None:
            validate_band(
                cleaned.get("accommodation"),
                cleaned["length_min_m"],
                cleaned.get("length_max_m"),
                exclude_pk=self.instance.pk,
            )
        return cleaned


class ServicePriceItemForm(BootstrapFormMixin, forms.ModelForm):
    """One row of the official service price list on the backoffice
    price list page. Currency stays EUR (OPEN-01), so it is not offered
    for editing."""

    class Meta:
        model = ServicePriceItem
        fields = [
            "category",
            "name",
            "equipment_category",
            "service_kind",
            "price",
            "unit",
            "is_active",
            "sort_order",
        ]
        widgets = {
            "price": forms.NumberInput(
                attrs={"step": "0.01"}
            ),
        }


class ApprovalLimitForm(BootstrapFormMixin, forms.ModelForm):
    """OPEN-01: one rung of the approval ladder (ACT-02/INV-04) on the
    backoffice configuration page, so management can enter the real
    amounts instead of them living only in the Django admin.

    Amounts are EUR (OPEN-01 currency decision, 2026-08-02);
    ``ApprovalLimit`` has no currency field, so no currency input is
    offered. Only the ladder roles are selectable - a limit on any other
    role would never be read by ``required_approval_levels()``. Cross-row
    integrity is delegated to ``accounts.services.validate_approval_limit``.
    """

    class Meta:
        model = ApprovalLimit
        fields = ["role", "max_amount"]
        widgets = {
            "max_amount": forms.NumberInput(
                attrs={
                    "step": "0.01",
                    "min": "0.01",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["role"]
        # Keep the blank choice, drop the non-ladder roles (ACT-02).
        field.choices = [
            choice
            for choice in field.choices
            if not choice[0] or choice[0] in LADDER_ROLES
        ]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("role") and cleaned.get("max_amount") is not None:
            validate_approval_limit(
                cleaned["role"],
                cleaned["max_amount"],
                exclude_pk=self.instance.pk,
            )
        return cleaned
