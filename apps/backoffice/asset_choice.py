"""The "existing asset" choice shared by the WNT-02 and TRP-01 intake forms.

Both forms create a Asset from their asset fields. For a client whose
asset is already on file - a SERVICE case identified it, or last season's
storage did - that produced a second record of the same outline, and the
client page then listed one asset twice, each with half the history. The
``existing_asset`` field goes in front of the create-a-asset fields:
chosen, it is linked and those fields are ignored.

Triage cannot do this for staff-entered cases either - its suggestions
carry an asset only when a registration number appears in the raw text -
so this is where a known asset gets attached to a winter or transport case.
"""
from django.utils.translation import gettext_lazy as _

from apps.clients.models import Asset


class ExistingAssetMixin:
    """Configure and validate ``existing_asset``; the forms declare the field.

    Declared on each form rather than here (a plain mixin's attributes are
    not collected by Django's form metaclass) so that its position among
    the other fields stays visible where the form is read.
    """

    def offer_existing_assets(self, case):
        """Scope the choice to what can sensibly be picked, or drop it.

        A case with a client offers that client's assets. A case whose
        client is chosen on this same form offers every asset, labelled
        with its owner - the pick then settles the client too. With
        nothing to choose the field is removed rather than rendered as
        an empty select.
        """
        field = self.fields["existing_asset"]
        if case is not None and case.client_id is not None:
            queryset = case.client.assets.order_by("name")
        else:
            queryset = Asset.objects.select_related("client").order_by(
                "client__name", "name"
            )
            field.label_from_instance = lambda asset: "%s — %s" % (
                asset,
                asset.client.name,
            )
        if not queryset.exists():
            del self.fields["existing_asset"]
            return
        field.queryset = queryset

    def reconcile_existing_asset(self, cleaned):
        """Reconcile the pick with the client section; adopt the owner.

        Not ``clean_existing_asset``: Django would take that for the
        field's own cleaner and call it without ``cleaned``.

        Runs before the client-or-name check so that an asset picked on
        its own supplies the client instead of tripping that check.
        """
        asset = cleaned.get("existing_asset")
        if asset is None or "existing_client" not in self.fields:
            return
        chosen = cleaned.get("existing_client")
        new_name = (cleaned.get("name") or "").strip()
        if chosen is not None and chosen.pk != asset.client_id:
            self.add_error(
                "existing_asset", _("This asset belongs to a different client.")
            )
        elif chosen is None and new_name:
            self.add_error(
                "existing_asset",
                _(
                    "This asset already has an owner - choose them as the "
                    "existing client instead of entering a new name."
                ),
            )
        elif chosen is None:
            cleaned["existing_client"] = asset.client
