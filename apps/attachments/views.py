"""Views for serving uploaded media behind authentication (POR-04).

Uploaded case photos contain client property/damage details and must
never be publicly reachable, so ``/media/...`` is routed through
``protected_media`` in all modes instead of Django's DEBUG-only static
serving.
"""

import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.views.static import serve

# Stored media names are random UUIDs (see attachment_upload_path), so the
# bytes at a given /media/ URL never change: cache them hard. ``private``
# keeps auth-protected client property out of shared proxy caches - only
# the requesting staff member's own browser retains it.
MEDIA_CACHE_CONTROL = "private, max-age=31536000, immutable"


@login_required
def protected_media(request, path):
    """Serve ``MEDIA_ROOT/path`` to authenticated users only.

    Behind nginx, set ``MEDIA_ACCEL_REDIRECT=True``: Django keeps the
    auth + traversal checks and answers with ``X-Accel-Redirect`` to an
    nginx ``internal`` location (``MEDIA_ACCEL_PREFIX``, see
    docker/nginx.conf.example) so nginx streams the file. With the flag
    off (default), Django streams via ``FileResponse``.

    Every branch carries an immutable ``Cache-Control`` header so
    revisited photos/PDFs load from the browser cache instead of being
    re-fetched. The auth check still runs on the first fetch per cache
    lifetime; the UUID names keep the URL unguessable in the meantime.
    """
    # Staff only, like every other page: a role-less account that is 403'd
    # everywhere else could still fetch any client photo by URL.
    if not request.user.is_staff:
        raise PermissionDenied
    media_root = Path(settings.MEDIA_ROOT).resolve()
    try:
        target = (media_root / path).resolve()
    except (OSError, ValueError):
        raise Http404("Invalid media path.")
    # POR-04: reject path traversal - the resolved target must stay
    # under MEDIA_ROOT.
    if target == media_root or media_root not in target.parents:
        raise Http404("Invalid media path.")

    if settings.DEBUG:
        response = serve(
            request,
            target.relative_to(media_root).as_posix(),
            document_root=settings.MEDIA_ROOT,
        )
        response["Cache-Control"] = MEDIA_CACHE_CONTROL
        return response

    if not target.is_file():
        raise Http404("Media file not found.")

    if getattr(settings, "MEDIA_ACCEL_REDIRECT", False):
        relative = target.relative_to(media_root).as_posix()
        content_type, _ = mimetypes.guess_type(target.name)
        response = HttpResponse(
            content_type=content_type or "application/octet-stream"
        )
        response["X-Accel-Redirect"] = settings.MEDIA_ACCEL_PREFIX + relative
        response["Cache-Control"] = MEDIA_CACHE_CONTROL
        return response

    response = FileResponse(target.open("rb"))
    response["Cache-Control"] = MEDIA_CACHE_CONTROL
    return response
