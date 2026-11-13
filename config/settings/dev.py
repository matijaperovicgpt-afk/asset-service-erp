"""Development settings for the erp project."""
from .base import *  # noqa: F401,F403
from .base import env

DEBUG = env.bool("DJANGO_DEBUG", default=True)

# Print outgoing emails to the console during development.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
