"""Root URL configuration for the erp project.

``base_patterns`` is everything except the client-facing portal;
``config.urls_internal`` reuses it for PORTAL_ENABLED=False deployments.
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django.utils.translation import gettext_lazy as _

from apps.accounts.views import RateLimitedLoginView, rate_limited_admin_login

from .views import healthz

admin.site.site_header = settings.SITE_NAME
admin.site.site_title = settings.SITE_NAME
admin.site.index_title = _("Administration")

base_patterns = [
    path("healthz", healthz, name="healthz"),
    # set_language for the SR/EN switcher (cookie-based, no URL prefixes).
    path("i18n/", include("django.conf.urls.i18n")),
    # Rate-limited login first (name "login" matches auth.urls), then the
    # rest of the stock auth flows: logout, password change/reset.
    path(
        "accounts/login/", RateLimitedLoginView.as_view(), name="login"
    ),
    path("accounts/", include("django.contrib.auth.urls")),
    # Ahead of admin.site.urls so it wins the match (QA R-M3).
    path("admin/login/", rate_limited_admin_login),
    path("admin/", admin.site.urls),
    path("backoffice/", include("apps.backoffice.urls")),
    # POR-04: uploaded media is private; served login-protected in ALL modes.
    path("", include("apps.attachments.urls")),
]

urlpatterns = base_patterns + [
    path("", include("apps.portal.urls")),
]
