#!/bin/bash
# Run by the official postgres image entrypoint on the FIRST start only (empty data directory), after
# initdb (POSTGRES_INITDB_ARGS: -E UTF8 --locale=C.UTF-8 --data-checksums) and after POSTGRES_DB was created.
# Creates the erp_owner / erp_app / erp_backup / erp_monitor roles from /almiras/sql/roles.sql with passwords
# read from Docker secret files. Nothing is printed that contains a password.
set -euo pipefail

read_secret() {
    local var="$1" path
    path="${!var:-}"
    if [[ -z "${path}" || ! -s "${path}" ]]; then
        echo "10-almiras-roles: ${var} must point at a non-empty secret file" >&2
        exit 1
    fi
    tr -d '\n' < "${path}"
}

owner_password="$(read_secret ALMIRAS_DB_OWNER_PASSWORD_FILE)"
app_password="$(read_secret ALMIRAS_DB_APP_PASSWORD_FILE)"
backup_password="$(read_secret ALMIRAS_DB_BACKUP_PASSWORD_FILE)"
monitor_password="$(read_secret ALMIRAS_DB_MONITOR_PASSWORD_FILE)"

psql --no-psqlrc -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" \
    -v owner_password="${owner_password}" \
    -v app_password="${app_password}" \
    -v backup_password="${backup_password}" \
    -v monitor_password="${monitor_password}" \
    -f /almiras/sql/roles.sql

echo "10-almiras-roles: roles erp_owner, erp_app, erp_backup, erp_monitor created in ${POSTGRES_DB}"
