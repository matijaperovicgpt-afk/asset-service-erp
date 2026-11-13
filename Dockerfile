FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# System dependencies: mysqlclient build/runtime, gettext for
# makemessages/compilemessages, curl for the healthcheck, ffmpeg for
# video derivatives - the package ships both ffmpeg and ffprobe, used
# by apps/attachments/video.py to extract poster frames and to write
# browser-playable H.264/AAC copies beside the untouched original.
# The worker runs this SAME image, so this one line covers both
# containers; conversely, deploying that code without rebuilding
# leaves every video at video_status=pending with only a log line to
# say so. python:3.12-slim is Debian 13 (trixie), which ships ffmpeg
# 7.1.5 - well past the force_divisible_by scale option the transcode
# uses (added in 4.4).
#
# MEASURED COST: +625 MB, taking the image from ~760 MB to ~1.42 GB.
# Debian's ffmpeg has no minimal variant and pulls ~205 packages
# (X11/SDL/codec libraries the CLI links but this app never exercises).
# That is the price of one apt line; if image pull time on deploy ever
# matters more than build simplicity, a static ffmpeg build copied in
# from a builder stage is roughly 80 MB for the same two binaries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        default-libmysqlclient-dev \
        pkg-config \
        gettext \
        curl \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

# Non-root runtime user; app dir writable for collectstatic/media.
RUN useradd --create-home app \
    && mkdir -p /app/staticfiles /app/media \
    && chown -R app:app /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# Sizing, timeouts and the worker class live in the config file so they
# are env-tunable (GUNICORN_WORKERS / GUNICORN_THREADS / ...) - an
# exec-form CMD cannot expand environment variables. See that file for
# why this runs gthread rather than gunicorn's default sync worker.
CMD ["gunicorn", "config.wsgi:application", "-c", "/app/docker/gunicorn.conf.py"]
