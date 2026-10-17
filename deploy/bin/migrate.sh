#!/bin/sh
# One-off migrate step (docker compose run --rm migrate). Runs with the OWNER role's credentials
# (POSTGRES_USER=erp_owner, POSTGRES_PASSWORD from the owner secret); the web and scheduler containers never get
# them (access.md R45). Steps:
#   1. manage.py migrate (creates/alters tables, functions and triggers, all owned by erp_owner)
#   2. re-apply the application-role grants and revoke the append-only rights (sql/grants.sql)
# Afterwards run the release check as the APPLICATION role (system checks, no unapplied migration, AT-47):
#   docker compose run --rm web release-check
set -eu

if [ "${POSTGRES_USER:-}" != "erp_owner" ]; then
    echo "migrate: POSTGRES_USER must be erp_owner (got '${POSTGRES_USER:-}')" >&2
    exit 64
fi

echo "migrate: applying migrations as ${POSTGRES_USER} on ${POSTGRES_HOST:-localhost}/${POSTGRES_DB:-}"
python manage.py migrate --noinput
python /opt/almiras/bin/dbrole.py grants
echo "migrate: done. Next: docker compose run --rm web release-check"
