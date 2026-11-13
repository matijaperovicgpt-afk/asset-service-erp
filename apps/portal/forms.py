"""Forms for the public client portal."""

from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from apps.attachments.models import VIDEO_EXTENSIONS as ALLOWED_VIDEO_EXTENSIONS

ALLOWED_PHOTO_EXTENSIONS = {"jpg", "jpeg", "png", "gif"}
MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10 MB per photo
MAX_VIDEO_SIZE = 50 * 1024 * 1024  # 50 MB per video
MAX_UPLOAD_COUNT = 5  # photos + videos combined

#: POR-05 client-side downscaling. Advisory only: the validators above
#: stay the sole authority (POR-04), and every failure path in the
#: browser uploads the original untouched. Long-edge cap and JPEG
#: quality applied before upload; anything under DOWNSCALE_MIN_BYTES is
#: left alone, since re-encoding a small photo often makes it bigger.
DOWNSCALE_MAX_EDGE = 2000
DOWNSCALE_QUALITY = 0.82
DOWNSCALE_MIN_BYTES = 1024 * 1024

# Locations offered by the location selector, from settings.LOCATION_CHOICES
# (the LOCATION_CHOICES env var, comma-separated). Proper nouns -
# intentionally not translated. "Other" is always appended.
LOCATION_CHOICES = [(name, name) for name in settings.LOCATION_CHOICES]
#: Sentinel choice value: "somewhere else" - resolved from location_other.
OTHER_LOCATION = "__other__"


class MultipleFileInput(forms.ClearableFileInput):
    """File input that accepts multiple files (Django 5 documented recipe)."""

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """FileField that cleans a list of uploaded files.

    Implements the multiple-file-upload recipe from the Django docs:
    override clean() to run the single-file clean over each file.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(item, initial) for item in data]
        elif data is None:
            result = []
        else:
            result = [single_file_clean(data, initial)]

        if len(result) > MAX_UPLOAD_COUNT:
            raise ValidationError(
                _("You can upload at most %(count)d files."),
                params={"count": MAX_UPLOAD_COUNT},
            )
        for uploaded in result:
            self._validate_upload(uploaded)
        return result

    @staticmethod
    def _validate_upload(uploaded):
        extension = uploaded.name.rsplit(".", 1)[-1].lower() if "." in uploaded.name else ""
        if extension in ALLOWED_PHOTO_EXTENSIONS:
            if uploaded.size > MAX_PHOTO_SIZE:
                raise ValidationError(
                    _("Photo %(name)s is too large. Maximum size is 10 MB."),
                    params={"name": uploaded.name},
                )
        elif extension in ALLOWED_VIDEO_EXTENSIONS:
            if uploaded.size > MAX_VIDEO_SIZE:
                raise ValidationError(
                    _("Video %(name)s is too large. Maximum size is 50 MB."),
                    params={"name": uploaded.name},
                )
        else:
            raise ValidationError(
                _(
                    "Unsupported file type: %(name)s. "
                    "Allowed: jpg, jpeg, png, gif, mp4, mov."
                ),
                params={"name": uploaded.name},
            )


class PublicReportForm(forms.Form):
    """Public damage/malfunction report form (no account required).

    Includes a hidden honeypot field named "website": real users never
    fill it, simple bots do. When it is filled we do NOT surface a form
    error; the view fakes a successful submission instead, so bots get
    no signal that they were detected and cannot tune their submissions.
    """

    contact_name = forms.CharField(
        label=_("Your name"),
        max_length=255,
    )
    phone = forms.CharField(
        label=_("Phone number"),
        max_length=50,
    )
    email = forms.EmailField(
        label=_("Email"),
        required=False,
        help_text=_("Optional - used to send you status updates."),
    )
    asset_name = forms.CharField(
        label=_("Asset name"),
        max_length=120,
    )
    asset_model = forms.CharField(
        label=_("Asset model"),
        max_length=120,
        required=False,
    )
    asset_length_m = forms.DecimalField(
        label=_("Asset length (m)"),
        required=False,
        max_digits=5,
        decimal_places=2,
        min_value=0,
    )
    site_position = forms.CharField(
        label=_("Position on site"),
        max_length=120,
        required=False,
    )
    asset_notes = forms.CharField(
        label=_("Additional asset details"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Registration number, engine type, anything else useful."),
    )
    # §7.3: unlike the client's own assessment (SVC-01), which the
    # portal only offers, this one is required here. It is a single
    # click from a curated list, the reporter always knows the answer,
    # and a fault report that does not say where the asset is cannot be
    # acted on at all.
    location = forms.ChoiceField(
        label=_("Location"),
    )
    location_other = forms.CharField(
        label=_("Other location"),
        max_length=255,
        required=False,
        help_text=_('Fill in only if you selected "Other" above.'),
    )
    symptoms_description = forms.CharField(
        label=_("Describe the problem"),
        widget=forms.Textarea(attrs={"rows": 5}),
    )
    # SVC-01 / §7.4: the client's own assessment is the second half of
    # the fault report, and it is offered here rather than demanded -
    # the public form is the one place where an extra mandatory question
    # costs a report that never gets sent (POR-01). Staff are refused at
    # the diagnostics gate if it never gets recorded.
    client_diagnosis = forms.CharField(
        label=_("What do you think is wrong? (optional)"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_(
            "Your own guess helps the technician - even a vague one. "
            "Leave it empty if you have no idea."
        ),
    )
    photos = MultipleFileField(
        label=_("Photos and videos"),
        required=False,
        help_text=_(
            "Up to 5 files: photos (jpg, png, gif) up to 10 MB, "
            "videos (mp4, mov) up to 50 MB."
        ),
    )
    # Honeypot - hidden from humans, checked in the view (fake success).
    website = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
        label="",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["location"].choices = (
            [("", _("Choose a location..."))]
            + LOCATION_CHOICES
            + [(OTHER_LOCATION, _("Other"))]
        )
        # Minimal Bootstrap styling for all visible widgets.
        for name, field in self.fields.items():
            if name == "website":
                continue
            css_class = (
                "form-select"
                if isinstance(field.widget, forms.Select)
                else "form-control"
            )
            existing = field.widget.attrs.get("class", "")
            field.widget.attrs["class"] = f"{existing} {css_class}".strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("location") == OTHER_LOCATION:
            other = (cleaned.get("location_other") or "").strip()
            if not other:
                self.add_error(
                    "location_other", _("Please describe the location.")
                )
            cleaned["location"] = other
        return cleaned

    def composed_asset_info(self) -> str:
        """Join the structured asset fields into the ``raw_asset_info``
        audit string (GEN-01).

        Label prefixes are fixed constants, NOT gettext: the composed
        string is stored data read by staff and substring-searched by
        GEN-03 triage - it must not vary with the submitter's UI
        language.
        """
        data = self.cleaned_data
        lines = ["Asset: %s" % data["asset_name"]]
        if data.get("asset_model"):
            lines.append("Model: %s" % data["asset_model"])
        if data.get("asset_length_m") is not None:
            lines.append("Length (m): %s" % data["asset_length_m"])
        if data.get("site_position"):
            lines.append("Position: %s" % data["site_position"])
        if data.get("asset_notes"):
            lines.append("Notes: %s" % data["asset_notes"])
        return "\n".join(lines)

    def is_honeypot_triggered(self) -> bool:
        """Return True if the hidden honeypot field was filled in."""
        return bool(self.data.get("website", "").strip())
