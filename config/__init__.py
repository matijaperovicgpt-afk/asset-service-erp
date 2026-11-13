"""Project package.

Importing the Celery app here is load-bearing, not cosmetic:
``@shared_task`` binds to whichever Celery app is current when the
decorated module is imported. Without this import the tasks would attach
to Celery's own anonymous default app, which knows nothing about Django
settings - so ``CELERY_TASK_ALWAYS_EAGER`` (the no-Redis fallback) would
be silently ignored by any bare ``.delay()``.
"""
from .celery import app as celery_app

__all__ = ("celery_app",)
