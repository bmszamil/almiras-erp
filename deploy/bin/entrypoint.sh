#!/bin/sh
# Entrypoint of the Almiras ERP image (deploy/Dockerfile).
#
# Secrets never sit in the compose file or the image. For each allowed variable NAME, if NAME_FILE points at a
# readable file (a Docker secret under /run/secrets), NAME is exported with the file's content. Settings keep
# reading plain environment variables (config/settings.py), so no settings code knows about files.
#
# Modes:
#   web        gunicorn on :8000 (copies the collected static files to /srv/static first)
#   scheduler  supercronic with /opt/almiras/crontab, after a catch-up run of the idempotent daily jobs
#   migrate    migrate as the owner role, then re-apply the application-role grants (bin/migrate.sh)
#   release-check  as the application role: manage.py check --deploy (ERROR level), migrate --check (nothing
#              unapplied) and the access R45 / AT-47 role check (bin/dbrole.py verify)
#   manage ... python manage.py ...
#   anything else is executed as given
set -eu

for name in DJANGO_SECRET_KEY POSTGRES_PASSWORD HR_PERSONAL_DATA_KEYS HR_PERSONAL_DATA_HASH_KEY \
            BANKING_DATA_KEYS WEB_MFA_KEYS DJANGO_EMAIL_PASSWORD ALMIRAS_ALERT_WEBHOOK_URL; do
    file_var="${name}_FILE"
    eval "file_path=\${${file_var}:-}"
    if [ -n "${file_path}" ]; then
        if [ ! -r "${file_path}" ]; then
            echo "entrypoint: ${file_var}=${file_path} is not readable" >&2
            exit 64
        fi
        # Strip one trailing newline (files written by editors); the value itself is never echoed.
        value="$(cat "${file_path}")"
        export "${name}=${value}"
        unset "${file_var}"
    fi
done
unset name file_var file_path value 2>/dev/null || true

cd /app

mode="${1:-web}"
case "${mode}" in
    web)
        shift || true
        if [ -d /srv/static ] && [ -w /srv/static ]; then
            cp -R /opt/almiras/static/. /srv/static/
        fi
        exec gunicorn config.wsgi:application \
            --bind "0.0.0.0:8000" \
            --workers "${GUNICORN_WORKERS:-3}" \
            --threads "${GUNICORN_THREADS:-1}" \
            --timeout "${GUNICORN_TIMEOUT:-120}" \
            --graceful-timeout 30 \
            --max-requests 2000 --max-requests-jitter 200 \
            --access-logfile - --error-logfile - \
            --forwarded-allow-ips "${GUNICORN_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
            "$@"
        ;;
    scheduler)
        shift || true
        exec /opt/almiras/bin/scheduler-start.sh "$@"
        ;;
    migrate)
        shift || true
        exec /opt/almiras/bin/migrate.sh "$@"
        ;;
    release-check)
        python manage.py check --deploy --fail-level ERROR
        python manage.py migrate --check --noinput > /dev/null
        echo "release-check: no unapplied migration"
        exec python /opt/almiras/bin/dbrole.py verify
        ;;
    manage)
        shift
        exec python manage.py "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
