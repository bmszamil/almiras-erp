#!/bin/bash
# Weekly physical base backup, only when WAL archiving is on (ALMIRAS_WAL_ARCHIVING=on). Together with the
# archived WAL in /backups/wal it allows point-in-time recovery (deploy/README.md "Point-in-time recovery").
# Tar format to stdout (-X fetch: the WAL needed for consistency is inside the tar), encrypted with age.
set -euo pipefail
# shellcheck source-path=SCRIPTDIR source=lib.sh
source "$(dirname "$0")/lib.sh"

if [[ "${ALMIRAS_WAL_ARCHIVING:-off}" != "on" ]]; then
    log "pg-basebackup: WAL archiving is off; nothing to do"
    exit 0
fi

: "${PGHOST:=db}" "${PGPORT:=5432}" "${PGUSER:=erp_backup}"
export PGHOST PGPORT PGUSER
if [[ -n "${PGPASSWORD_FILE:-}" ]]; then
    PGPASSWORD="$(tr -d '\n' < "${PGPASSWORD_FILE}")"
    export PGPASSWORD
fi
keep_days="${BACKUP_RETENTION_BASE_DAYS:-15}"
base_dir="${BACKUP_ROOT}/pg/base"
wal_dir="${BACKUP_ROOT}/wal"

fail() {
    log "pg-basebackup: FAILED: $*"
    backup_metric pg_basebackup 1
    exit 1
}
trap 'fail "line ${LINENO}"' ERR

require_recipients || fail "no recipients"
mkdir -p "${base_dir}"
stamp="$(date +%Y%m%dT%H%M%S%z)"
out="${base_dir}/base-${stamp}.tar.age"
tmp="${base_dir}/.base-${stamp}.partial"

pg_basebackup --pgdata=- --format=tar --wal-method=fetch --checkpoint=fast --no-password \
    --label="almiras-${stamp}" \
    | age --encrypt --recipients-file "${BACKUP_RECIPIENTS}" --output "${tmp}"
[[ -s "${tmp}" ]] || fail "empty base backup"
sync "${tmp}"
mv "${tmp}" "${out}"
(cd "${base_dir}" && sha256sum "$(basename "${out}")" > "$(basename "${out}").sha256")

prune "${base_dir}" "base-*.tar.age" "${keep_days}" || true
prune "${base_dir}" "base-*.tar.age.sha256" "${keep_days}" || true
# WAL older than the oldest kept base backup is no longer useful for recovery.
oldest_base="$(find "${base_dir}" -maxdepth 1 -name 'base-*.tar.age' -printf '%T@ %p\n' | sort -n | head -1 | cut -d' ' -f2-)"
if [[ -n "${oldest_base}" && -d "${wal_dir}" ]]; then
    find "${wal_dir}" -maxdepth 1 -type f -name '*.age' ! -newer "${oldest_base}" -print -delete || true
fi

backup_metric pg_basebackup 0 "$(stat -c %s "${out}")"
log "pg-basebackup: OK ${out}"
