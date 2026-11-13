"""Infrastructure views (not domain logic)."""
import logging

from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def healthz(request):
    """Liveness/readiness probe: verifies the database answers.

    Unauthenticated by design - load balancers and Docker healthchecks
    call it; it leaks nothing but an ok/error flag.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        logger.exception("Health check failed")
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})
