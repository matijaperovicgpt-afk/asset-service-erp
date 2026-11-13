"""Response hardening for signed-in pages (QA UI-M6).

Every page a signed-in user sees carries client names, phone numbers,
asset and price details. Without a ``Cache-Control`` header browsers are
free to keep those pages in their HTTP cache and back/forward cache, so
pressing Back after signing out redisplayed them. ``no-store`` stops that.
"""

from django.conf import settings
from django.utils.cache import add_never_cache_headers


class PrivateNoStoreMiddleware:
    """Mark responses to authenticated users as never-cacheable.

    Leaves alone:

    * any response that already chose its own ``Cache-Control`` - the
      protected media view deliberately sends ``private, max-age=...,
      immutable`` for UUID-named uploads;
    * anything under ``MEDIA_URL`` or ``STATIC_URL`` (media errors and
      static files keep their normal caching);
    * anonymous responses (the sign-in page, the public portal).

    Must sit after ``AuthenticationMiddleware`` so ``request.user`` exists.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.skip_prefixes = tuple(
            prefix
            for prefix in (settings.MEDIA_URL, settings.STATIC_URL)
            if prefix and prefix.startswith("/")
        )

    def __call__(self, request):
        response = self.get_response(request)
        if response.has_header("Cache-Control"):
            return response
        if self.skip_prefixes and request.path.startswith(self.skip_prefixes):
            return response
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return response
        add_never_cache_headers(response)
        return response
