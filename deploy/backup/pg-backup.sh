#!/bin/bash
# Nightly logical backup of the Almiras ERP database (custom format, pg_dump -Fc), encrypted with age to the
# owner's PUBLIC key(s); the private key never sits on this server. All copies stay in KSA (zatca D-5, R-43).
#
#   /backups/pg/daily/almiras-<YYYYmmddTHHMMSS+0300>.dump.age   (+ .sha256)
#   /backups/pg/monthly/...                                    (copy of the first dump of each month)
#
# Connects as erp_backup (read-only). Env: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSFILE or PGPASSWORD_FILE,
# BACKUP_RETENTION_DAILY_DAYS (35), BACKUP_RETENTION_MONTHLY_DAYS (400).
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

: "${PGHOST:=db}" "${PGPORT:=5432}" "${PGDATABASE:=almiras}" "${PGUSER:=erp_backup}"
export PGHOST PGPORT PGDATABASE PGUSER
if [[ -n "${PGPASSWORD_FILE:-}" ]]; then
    PGPASSWORD="$(tr -d '\n' < "${PGPASSWORD_FILE}")"
    export PGPASSWORD
fi
daily_days="${BACKUP_RETENTION_DAILY_DAYS:-35}"
monthly_days="${BACKUP_RETENTION_MONTHLY_DAYS:-400}"
daily_dir="${BACKUP_ROOT}/pg/daily"
monthly_dir="${BACKUP_ROOT}/pg/monthly"

fail() {
    log "pg-backup: FAILED: $*"
    backup_metric pg_dump 1
    exit 1
}
trap 'fail "line ${LINENO}"' ERR

require_recipients || fail "no recipients"
mkdir -p "${daily_dir}" "${monthly_dir}"

stamp="$(date +%Y%m%dT%H%M%S%z)"
out="${daily_dir}/${PGDATABASE}-${stamp}.dump.age"
tmp="${daily_dir}/.${PGDATABASE}-${stamp}.partial"

log "pg-backup: dumping ${PGDATABASE} from ${PGHOST} as ${PGUSER}"
# pipefail: a pg_dump error fails the pipeline even though age succeeds.
pg_dump --format=custom --compress=6 --no-password --serializable-deferrable \
    | age --encrypt --recipients-file "${BACKUP_RECIPIENTS}" --output "${tmp}"
[[ -s "${tmp}" ]] || fail "empty dump"
sync "${tmp}"
mv "${tmp}" "${out}"
(cd "${daily_dir}" && sha256sum "$(basename "${out}")" > "$(basename "${out}").sha256")

month_tag="$(date +%Y%m)"
if ! compgen -G "${monthly_dir}/${PGDATABASE}-${month_tag}*.dump.age" > /dev/null; then
    cp -p "${out}" "${out}.sha256" "${monthly_dir}/"
fi

prune "${daily_dir}" "*.dump.age" "${daily_days}" || true
prune "${daily_dir}" "*.dump.age.sha256" "${daily_days}" || true
prune "${monthly_dir}" "*.dump.age" "${monthly_days}" || true
prune "${monthly_dir}" "*.dump.age.sha256" "${monthly_days}" || true

size="$(stat -c %s "${out}")"
backup_metric pg_dump 0 "${size}"
log "pg-backup: OK ${out} (${size} bytes)"
