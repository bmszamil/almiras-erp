#!/bin/bash
# Restore drill (run on the KSA host, from deploy/, monthly and before every go-live / upgrade):
#   1. verifies the checksum of a pg_dump backup (default: the newest daily one),
#   2. decrypts it with the owner's age IDENTITY (private key, brought to the drill, never stored on the server),
#   3. restores it into a THROWAWAY PostgreSQL ("restore-db", compose profile "drill": tmpfs, own network,
#      no published port, gone afterwards) with the same roles as production,
#   4. runs backup/smoke.sql (tables, triggers, entities, every posted entry balanced, trial-balance totals),
#   5. runs, as the application role erp_app against the restored copy: manage.py check --deploy, migrate --check
#      and the access R45 / AT-47 role check (entrypoint "release-check"),
#   6. tears the throwaway database down and records the result for Prometheus.
#
# Usage: ALMIRAS_RESTORE_IDENTITY=/media/usb/almiras-backup.agekey ./backup/restore-test.sh [path/to/dump.age]
# Never point this at the production database: it only talks to the "restore-db" service.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
cd "${here}"
compose=(docker compose --profile drill)
backup_dir="${ALMIRAS_BACKUP_DIR:-/srv/almiras/backups}"
identity="${ALMIRAS_RESTORE_IDENTITY:?set ALMIRAS_RESTORE_IDENTITY to the age identity file (kept offline)}"
dump="${1:-}"
if [[ -z "${dump}" ]]; then
    dump="$(find "${backup_dir}/pg/daily" -maxdepth 1 -name '*.dump.age' -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
fi
[[ -n "${dump}" && -s "${dump}" ]] || { echo "restore-test: no dump found" >&2; exit 1; }

status=1
cleanup() {
    "${compose[@]}" rm --stop --force --volumes restore-db > /dev/null 2>&1 || true
    "${compose[@]}" run --rm --no-deps -T restore-check \
        sh -c "printf '%s\n' '# TYPE almiras_restore_test_last_status gauge' \
               'almiras_restore_test_last_status ${status}' \
               '# TYPE almiras_restore_test_last_run_timestamp_seconds gauge' \
               'almiras_restore_test_last_run_timestamp_seconds $(date +%s)' \
               > /var/lib/almiras/metrics/restore_test.prom.tmp \
               && mv /var/lib/almiras/metrics/restore_test.prom.tmp /var/lib/almiras/metrics/restore_test.prom" \
        > /dev/null 2>&1 || true
    if [[ "${status}" == "0" ]]; then echo "restore-test: PASSED (${dump})"; else echo "restore-test: FAILED (${dump})" >&2; fi
}
trap cleanup EXIT

echo "restore-test: checking ${dump}"
if [[ -f "${dump}.sha256" ]]; then
    (cd "$(dirname "${dump}")" && sha256sum --check --status "$(basename "${dump}").sha256")
else
    echo "restore-test: WARNING no .sha256 next to the dump" >&2
fi

decrypt() {
    if command -v age > /dev/null 2>&1; then
        age --decrypt --identity "${identity}" "${dump}"
    else
        # No age on the host: use the one in the almiras-postgres image (identity mounted read-only).
        docker run --rm -i --network none -v "${identity}:/identity:ro" -v "${dump}:/dump.age:ro" \
            --entrypoint age "${ALMIRAS_POSTGRES_IMAGE:-almiras-postgres:16}" --decrypt --identity /identity /dump.age
    fi
}

"${compose[@]}" up --detach --wait restore-db
echo "restore-test: restoring into the throwaway database"
decrypt | "${compose[@]}" exec -T --user postgres restore-db \
    pg_restore --username postgres --dbname almiras --exit-on-error --single-transaction --no-password
echo "restore-test: smoke queries"
"${compose[@]}" exec -T --user postgres restore-db psql --no-psqlrc --username postgres --dbname almiras -v ON_ERROR_STOP=1 \
    -f /almiras/backup/smoke.sql
echo "restore-test: Django checks as erp_app on the restored copy"
"${compose[@]}" run --rm --no-deps restore-check release-check
"${compose[@]}" run --rm --no-deps restore-check manage check
status=0
