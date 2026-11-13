"""Celery application for the erp project.

Background jobs are opt-in. The broker URL is derived from ``REDIS_URL``
(see ``config/settings/base.py``); when that is unset - the Windows dev
box, CI and the test suite - ``CELERY_TASK_ALWAYS_EAGER`` is True and the
enqueue helpers in the apps run their task inline at the call site, so
the app behaves exactly as it did before background jobs existed. The
app object is built either way, so task registration and autodiscovery
are identical with and without Redis.

There is deliberately no result backend: nothing calls ``.get()``, and
storing results would consume the memory the queue needs (the Redis
instance runs ``maxmemory-policy noeviction``). A future caller that
wants a result must add one first, or it will hang.

Worker (production, same image as web)::

    celery -A config worker -l info --concurrency 2

Celery's prefork pool does not work on Windows; a developer
experimenting with a local Redis must use ``--pool=solo``.
"""
import os

from celery import Celery

# Matches config/wsgi.py and config/asgi.py: the image bakes prod
# settings, and dev/test set DJANGO_SETTINGS_MODULE themselves.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

app = Celery("erp")

# One configuration surface: every Celery option is a CELERY_* Django
# setting, so per-environment differences live where all the others do.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Picks up apps/<app>/tasks.py for every entry in INSTALLED_APPS.
app.autodiscover_tasks()
