"""The client + asset half shared by the WNT-02 and TRP-01 intake pages.

Both ``storage.py`` and ``transport.py`` open with the same staff form:
pick or create the Client, pick or create the Asset, then the
department's own fields. The two copies had drifted apart once already
(``formmixins`` records the widget-class loops disagreeing), so the shared
part lives here once:

* ``ClientAssetIntakeForm`` - the contact and asset-identity fields,
  the ``__init__(case=...)`` that drops the sections a linked case already
  covers, and the ``clean()`` that settles existing-vs-new for client and
  asset and resolves the "Other" site free text.
* ``create_or_link_client_asset`` - the view-side block that turns the
  cleaned data into Client/Asset rows and attaches them through
  ``link_case`` (so the ``raw_*`` audit data is never modified, GEN-03).

What stays in each module is what genuinely differs: ``asset_length_m``
(its help text, and in transport the brand/model fields that precede it),
the ``origin_site`` label, and everything after the asset section.
"""
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .formmixins import BootstrapFormMixin
from .asset_choice import ExistingAssetMixin

from apps.clients.models import Client, Asset
from apps.clients.services import link_case
from apps.portal.forms import OTHER_LOCATION, LOCATION_CHOICES

#: The client section, dropped when the case already has a client.
CLIENT_FIELDS = ("existing_client", "name", "phone", "email")
#: The asset-identity section, dropped when the case already has an asset.
#: ``asset_length_m`` is deliberately not here: it always stays, because
#: both departments need the length even for a known asset.
ASSET_FIELDS = ("existing_asset", "asset_name", "asset_type", "asset_registration")


class ClientAssetIntakeForm(ExistingAssetMixin, BootstrapFormMixin, forms.Form):
    """Client + asset identity for a staff intake form.

    Subclasses declare their own fields after these (Django keeps base
    fields first, so the page order is unchanged) - including
    ``asset_length_m`` and ``origin_site`` / ``origin_site_other``,
    whose labels and position differ per department. When the two site
    fields are declared, this class wires the port choices and the "Other"
    free text for them.
    """
    # A full-page form, not one of the dense case-detail cards.
    bootstrap_size = None


    existing_client = forms.ModelChoiceField(
        label=_("Existing client"),
        queryset=Client.objects.order_by("name"),
        required=False,
        empty_label=_("— create a new client —"),
    )
    name = forms.CharField(label=_("Client name"), max_length=255, required=False)
    phone = forms.CharField(label=_("Phone"), max_length=50, required=False)
    email = forms.EmailField(label=_("Email"), required=False)

    # Queryset and visibility are settled per case by ExistingAssetMixin.
    existing_asset = forms.ModelChoiceField(
        label=_("Existing asset"),
        queryset=Asset.objects.none(),
        required=False,
        empty_label=_("— create a new asset —"),
        help_text=_(
            "Pick the client's asset if it is already on file - the fields "
            "below then stay empty."
        ),
    )
    asset_name = forms.CharField(
        label=_("Asset name"), max_length=255, required=False
    )
    asset_type = forms.CharField(
        label=_("Asset type"), max_length=100, required=False
    )
    asset_registration = forms.CharField(
        label=_("Asset registration number"), max_length=50, required=False
    )

    def __init__(self, *args, case=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.case = case

        if case is not None and case.client_id is not None:
            for field in CLIENT_FIELDS:
                del self.fields[field]
        if case is not None and case.asset_id is not None:
            for field in ASSET_FIELDS:
                del self.fields[field]
        else:
            self.offer_existing_assets(case)

        if "origin_site" in self.fields:
            self.fields["origin_site"].choices = (
                [("", _("Choose a site..."))]
                + LOCATION_CHOICES
                + [(OTHER_LOCATION, _("Other"))]
            )

    def clean(self):
        cleaned = super().clean()

        self.reconcile_existing_asset(cleaned)
        if "existing_client" in self.fields:
            existing = cleaned.get("existing_client")
            new_name = (cleaned.get("name") or "").strip()
            if existing and new_name:
                self.add_error(
                    "name",
                    _("Choose an existing client OR enter a new name, not both."),
                )
            if not existing and not new_name:
                self.add_error(
                    "name", _("Select an existing client or enter a new name.")
                )

        if (
            "asset_name" in self.fields
            and cleaned.get("existing_asset") is None
            and not (cleaned.get("asset_name") or "").strip()
        ):
            self.add_error("asset_name", _("Asset name is required."))

        if (
            "origin_site" in self.fields
            and cleaned.get("origin_site") == OTHER_LOCATION
        ):
            other = (cleaned.get("origin_site_other") or "").strip()
            if not other:
                self.add_error(
                    "origin_site_other", _("Please name the site / port.")
                )
            cleaned["origin_site"] = other
        return cleaned


def create_or_link_client_asset(case, form, *, user):
    """Resolve the form's client + asset into rows and attach them to ``case``.

    Call inside the view's ``transaction.atomic()`` with a valid ``form``.
    A client/asset the case already has wins over the form; otherwise the
    picked existing row is used, and failing that one is created from the
    form fields. Returns ``(client, asset)``.

    Raises ``ValidationError`` (after recording the error on the form, so
    the view re-renders it in place) when the asset belongs to a client
    other than the one resolved.
    """
    data = form.cleaned_data
    client = case.client or data.get("existing_client")
    if client is None:
        client = Client.objects.create(
            name=data["name"].strip(),
            phone=data.get("phone", ""),
            email=data.get("email", ""),
        )
    asset = case.asset or data.get("existing_asset")
    if asset is None:
        asset = Asset.objects.create(
            client=client,
            name=data["asset_name"].strip(),
            asset_type=data.get("asset_type", ""),
            registration_no=data.get("asset_registration", ""),
            length_m=data["asset_length_m"],
        )
    needs_link = case.client_id != client.pk or case.asset_id != asset.pk
    if needs_link and asset.client_id != client.pk:
        # Refused before anything is written, so the function is safe even
        # outside the caller's transaction.
        form.add_error(
            "existing_client",
            _("The case's asset belongs to a different client."),
        )
        raise ValidationError(form.errors.as_text())
    if asset.length_m != data["asset_length_m"]:
        # The length field is prefilled from the asset and reads as the
        # asset's length, but it only ever reached the newly created
        # branch - so correcting it here left the asset record, the
        # asset card beside it and the transport mode recommendation on
        # the old number.
        asset.length_m = data["asset_length_m"]
        asset.save(update_fields=["length_m"])
    if needs_link:
        link_case(case, client, asset=asset, user=user)
    return client, asset
