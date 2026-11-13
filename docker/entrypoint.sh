#!/bin/sh
# Container entrypoint: collect static assets, optionally migrate, then
# exec the CMD (gunicorn by default).
set -e

# The worker container shares this image but must not touch the shared
# static volume - collectstatic there races the web container.
if [ "${DJANGO_COLLECTSTATIC:-1}" = "1" ]; then
    # -i README.md: any README dropped into static/ is a note to the next
    # developer, not an asset - without this it would be copied, hashed and
    # published under /static/. Django has no settings-level equivalent.
    python manage.py collectstatic --noinput -i README.md
fi

# Migrations run only when explicitly requested - with several replicas
# you want exactly one migrator, not a race on startup.
if [ "${DJANGO_MIGRATE_ON_START:-0}" = "1" ]; then
    python manage.py migrate --noinput
fi

exec "$@"
