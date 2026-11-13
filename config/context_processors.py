"""Template context shared by every page."""
from django.conf import settings


def branding(request):
    """Expose the configurable product name as ``{{ SITE_NAME }}``."""
    return {"SITE_NAME": settings.SITE_NAME}
