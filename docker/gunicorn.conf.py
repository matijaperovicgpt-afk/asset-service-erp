"""Gunicorn configuration for the ERP web container.

Exists because the settings below must be env-tunable and an exec-form
``CMD`` cannot expand environment variables. Referenced from the
Dockerfile as ``gunicorn config.wsgi:application -c
/app/docker/gunicorn.conf.py``.

WHY gthread AND NOT THE DEFAULT sync WORKER
-------------------------------------------
A sync worker serves exactly one request at a time, start to finish. The
image previously ran three of them, so the whole application could serve
**three concurrent requests** - and "concurrent" counts the whole
lifetime of a request, including the seconds or minutes a browser spends
pulling a 50 MB video down a phone connection. Three people watching
damage videos meant nobody else could load a page at all.

This app is almost entirely I/O-bound: it waits on MySQL, on SMTP, on
the filesystem, and on the client's own connection. Those are exactly
the waits a thread can yield through, so ``gthread`` multiplies capacity
by ``threads`` at nearly no memory cost, because threads share the
worker's Python heap. Django is thread-safe, the app holds no
module-level mutable state that matters (the one log-once set in
``apps/attachments/video.py`` can at worst log a duplicate line), and
every database connection is already per-thread.

Concurrency is therefore ``workers x threads``: 3 x 4 = 12 by default,
up from 3, on the same box and roughly the same RAM.

Note the division of labour, which is the reason this is enough:
- Long file transfers belong to nginx once ``MEDIA_ACCEL_REDIRECT`` is
  on - Django does the auth check and hands off in milliseconds.
- Minute-long work (video transcoding) belongs to the Celery worker and
  is never allowed on a request path at all.
Neither of those is what gunicorn is sized for; this sizing is for
ordinary page rendering.
"""
import os

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# Processes. The textbook formula is (2 x CPU) + 1, but this box also
# runs MySQL, Redis and the Celery worker, so it is left at the value the
# deployment has always used rather than grown automatically - raise it
# deliberately, and watch RSS when you do (each worker is a full Python
# process, ~80-120 MB here).
workers = int(os.environ.get("GUNICORN_WORKERS", "3"))

# Threads per worker. This is the cheap axis: +1 thread costs a stack and
# a database connection, not a Python interpreter.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "gthread")
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# How long a single request may run before the worker is killed and
# restarted. Gunicorn's default of 30 s is dangerous for this app: with
# MEDIA_ACCEL_REDIRECT off, Django itself streams the file, so a 50 MB
# video to a phone on site wifi routinely exceeds 30 s and the worker
# is killed MID-DOWNLOAD - the client sees a truncated file and the log
# shows a worker timeout, not an error anyone would connect to media.
# Turning accel-redirect on removes the cause; this raises the ceiling
# either way.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))

# Let in-flight requests finish on redeploy instead of being cut off.
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# Hold idle keep-alive connections a little longer than the default 2 s:
# nginx is the only client, and recycling that connection per page load
# is wasted handshakes.
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# Recycle workers periodically so any slow leak is bounded - Pillow in
# particular holds decoded bitmaps. Same reasoning as the Celery worker's
# CELERY_WORKER_MAX_TASKS_PER_CHILD. The jitter keeps all three workers
# from recycling on the same request.
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "100"))

# Logs to stdout/stderr for `docker compose logs`, matching the Django
# LOGGING config. Access logging is off by default: nginx already writes
# an access log for the same requests, and duplicating it doubles the
# noise for no extra information. Set GUNICORN_ACCESS_LOG=- to enable.
accesslog = os.environ.get("GUNICORN_ACCESS_LOG") or None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# Trust the forwarded scheme from the host nginx, which terminates TLS.
# Without this Django builds http:// absolute URLs behind an https site.
forwarded_allow_ips = os.environ.get("GUNICORN_FORWARDED_ALLOW_IPS", "*")
