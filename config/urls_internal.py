"""Root URL configuration without the client-facing portal.

Selected by ``ROOT_URLCONF`` when ``PORTAL_ENABLED`` is False (see
config/settings/base.py). Everything staff use is unchanged. "/" goes to
the backoffice, whose StaffRequiredMixin turns that into the sign-in page
for anyone not logged in - one page for staff either way, rather than a
login form shown again to someone already signed in. The portal routes
(the report form at "/" and its thank-you page) simply do not exist here:
404, not 403, so the deployment reveals nothing about a client side it
does not have.
"""
from django.urls import path
from django.views.generic import RedirectView

from .urls import base_patterns

urlpatterns = base_patterns + [
    path("", RedirectView.as_view(pattern_name="backoffice:home"), name="root"),
]
