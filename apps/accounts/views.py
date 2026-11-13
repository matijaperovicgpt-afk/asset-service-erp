"""Staff authentication views.

The backoffice used the Django admin login until now; this login view is
rate-limited per IP (django-ratelimit, same backend the POR-04 portal
limit uses) so password guessing gets a 403 instead of infinite tries.
"""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.utils.decorators import method_decorator
from django_ratelimit.decorators import ratelimit

LOGIN_RATE_LIMIT = "10/m"

#: Both doors into a staff account share one bucket, so switching from
#: one to the other does not buy a fresh ten tries.
LOGIN_RATE_GROUP = "staff-login"

_login_limit = ratelimit(
    group=LOGIN_RATE_GROUP,
    key="ip",
    rate=LOGIN_RATE_LIMIT,
    method="POST",
    block=True,
)


@method_decorator(_login_limit, name="post")
class RateLimitedLoginView(auth_views.LoginView):
    """Standard LoginView; more than 10 POSTs/minute per IP -> 403."""


def proxied_client_ip(request):
    """The client address nginx saw (it overwrites ``X-Real-IP``), else
    the socket peer. Used as ``RATELIMIT_IP_META_KEY`` in prod, where the
    peer is always the docker gateway (QA R-M4)."""
    return request.META.get("HTTP_X_REAL_IP") or request.META["REMOTE_ADDR"]


def rate_limited_admin_login(request, extra_context=None):
    """``/admin/login/`` with the same limit as the backoffice login.

    It checks the same passwords, and it was not throttled at all: 15 bad
    POSTs in a few seconds all came back 200 (QA R-M3)."""
    return _login_limit(admin.site.login)(request, extra_context=extra_context)
