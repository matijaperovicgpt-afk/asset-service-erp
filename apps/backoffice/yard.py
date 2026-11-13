"""WNT-09: the yard editor - the real yard as a picture under the plan.

The graphic plan (WNT-05) drew every zone as a bare rectangle, and the only
way to change a zone was the Django admin. This page gives a manager one
form per zone: its name, its bounding rectangle in metres, the working
clearance, and a site plan - an aerial photo or a drawing of the yard -
that is stretched to that rectangle and drawn under the grid. Assets are
placed over the picture; the picture itself is never measured.

Nothing here judges a placement. The rules stay in
``storage.services``; this page only edits the facts they read.
Reserved ground (obstacles, WNT-08) is still kept in the admin.
"""

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import TemplateView

from apps.backoffice.views import (
    PRICING_ROLES,
    StaffRequiredMixin,
    _may_edit_pricing,
    build_zone_views,
    zone_payload,
)
from apps.storage.models import StorageZone
from apps.storage.services import zone_conflicts

from .formmixins import BootstrapFormMixin

#: Who may change the yard. The same managerial pair that edits the
#: price lists: the plan's rules are only as true as its geometry, and a
#: rectangle shrunk by mistake un-parks every asset outside it.
YARD_EDIT_ROLES = PRICING_ROLES


def _may_edit_yard(user) -> bool:
    return _may_edit_pricing(user)


class StorageZoneForm(BootstrapFormMixin, forms.ModelForm):
    bootstrap_size = None

    class Meta:
        model = StorageZone
        fields = [
            "name",
            "width_m",
            "length_m",
            "clearance_m",
            "plan_image",
            "is_active",
            "sort_order",
        ]

    def clean(self):
        cleaned = super().clean()
        # The model carries no floor on the rectangle, and a zero or
        # negative side makes a plan nothing can be placed on.
        for key in ("width_m", "length_m"):
            value = cleaned.get(key)
            if value is not None and value <= 0:
                self.add_error(key, _("Must be greater than zero."))
        return cleaned


class StorageZoneEditView(StaffRequiredMixin, TemplateView):
    """WNT-09: create or change a storage zone.

    GET draws the form beside a preview of the plan: the site plan under
    the grid, the parked assets and the reserved ground, all read-only.
    POST saves the form and reports any parked asset the new rectangle no
    longer holds.
    """

    template_name = "backoffice/storage_zone_edit.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and request.user.is_staff and not (
            _may_edit_yard(request.user)
        ):
            messages.error(
                request,
                _("Role '%(role)s' may not change the yard (allowed: %(allowed)s).")
                % {
                    "role": request.user.role or _("(none)"),
                    "allowed": ", ".join(YARD_EDIT_ROLES),
                },
            )
            return redirect("backoffice:storage-layout")
        return super().dispatch(request, *args, **kwargs)

    def get_zone(self):
        pk = self.kwargs.get("pk")
        if pk is None:
            return None
        return get_object_or_404(
            StorageZone.objects.prefetch_related(
                "obstacles", "positions__agreement__case__client"
            ),
            pk=pk,
        )

    def get(self, request, *args, **kwargs):
        zone = self.get_zone()
        return self.render_editor(StorageZoneForm(instance=zone), zone)

    def render_editor(self, form, zone):
        return self.render_to_response(self.get_context_data(form=form, zone=zone))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        zone = context.get("zone")
        assets = []
        obstacles = []
        plan_image_url = ""
        if zone is not None:
            views = build_zone_views([zone])
            payload = zone_payload(views)[0]
            assets = payload["assets"]
            obstacles = payload["obstacles"]
            if zone.plan_image:
                plan_image_url = zone.plan_image.url
        # What the preview draws. The assets and obstacles are shown so the
        # planner sees what a smaller rectangle would cut off.
        context["editor_data"] = {
            "assets": assets,
            "obstacles": obstacles,
            "planImage": plan_image_url,
        }
        return context

    def post(self, request, *args, **kwargs):
        zone = self.get_zone()
        form = StorageZoneForm(request.POST, request.FILES, instance=zone)
        if not form.is_valid():
            messages.error(
                request, _("Check the highlighted fields - the yard was not saved.")
            )
            return self.render_editor(form, zone)
        saved = form.save()
        messages.success(request, _("Yard %(name)s saved.") % {"name": saved.name})
        # The assets do not move when the rectangle does. Say which ones no
        # longer fit, so they are re-placed on purpose.
        conflicts = zone_conflicts(saved)
        if conflicts:
            messages.warning(
                request,
                _(
                    "%(n)d placed asset(s) no longer fit the redrawn yard "
                    "and must be moved: %(list)s."
                )
                % {
                    "n": len(conflicts),
                    "list": "; ".join(
                        "#%s %s" % (position.agreement.case_id, reason)
                        for position, reason in conflicts
                    ),
                },
            )
        return redirect(reverse("backoffice:storage-zone-edit", args=[saved.pk]))
