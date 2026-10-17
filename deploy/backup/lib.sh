#!/bin/bash
# Shared helpers for the backup scripts (sourced). Metrics go to the node-exporter textfile collector
# directory so Prometheus alerts on a missed or failed backup (monitoring/alert-rules.yml).

ALMIRAS_METRICS_DIR="${ALMIRAS_METRICS_DIR:-/metrics}"
BACKUP_ROOT="${BACKUP_ROOT:-/backups}"
BACKUP_RECIPIENTS="${ALMIRAS_BACKUP_RECIPIENTS:-/almiras/backup-recipients.txt}"

log() {
    printf '%s %s\n' "$(date --iso-8601=seconds)" "$*" >&2
}

# backup_metric <kind> <status 0|1> [size_bytes]
backup_metric() {
    local kind="$1" status="$2" size="${3:-0}" file tmp now
    [[ -d "${ALMIRAS_METRICS_DIR}" && -w "${ALMIRAS_METRICS_DIR}" ]] || return 0
    file="${ALMIRAS_METRICS_DIR}/backup_${kind}.prom"
    tmp="$(mktemp "${ALMIRAS_METRICS_DIR}/.backup_${kind}.XXXXXX")"
    now="$(date +%s)"
    {
        echo "# HELP almiras_backup_last_run_timestamp_seconds Last time this backup ran."
        echo "# TYPE almiras_backup_last_run_timestamp_seconds gauge"
        echo "almiras_backup_last_run_timestamp_seconds{kind=\"${kind}\"} ${now}"
        echo "# HELP almiras_backup_last_status 0 = last run succeeded, 1 = failed."
        echo "# TYPE almiras_backup_last_status gauge"
        echo "almiras_backup_last_status{kind=\"${kind}\"} ${status}"
        if [[ "${status}" == "0" ]]; then
            echo "# HELP almiras_backup_last_success_timestamp_seconds Last successful backup."
            echo "# TYPE almiras_backup_last_success_timestamp_seconds gauge"
            echo "almiras_backup_last_success_timestamp_seconds{kind=\"${kind}\"} ${now}"
            echo "# HELP almiras_backup_last_size_bytes Size of the last successful backup file."
            echo "# TYPE almiras_backup_last_size_bytes gauge"
            echo "almiras_backup_last_size_bytes{kind=\"${kind}\"} ${size}"
        elif [[ -f "${file}" ]]; then
            # Keep the last success timestamp so "no success for 26 h" still fires correctly.
            grep -E '^almiras_backup_last_(success_timestamp_seconds|size_bytes)' "${file}" || true
        fi
    } > "${tmp}"
    chmod 0644 "${tmp}"
    mv "${tmp}" "${file}"
}

require_recipients() {
    if [[ ! -s "${BACKUP_RECIPIENTS}" ]] || ! grep -qE '^(age1|ssh-)' "${BACKUP_RECIPIENTS}"; then
        log "no age recipient public key in ${BACKUP_RECIPIENTS}; refusing to write an unencrypted backup"
        return 1
    fi
}

# prune <dir> <glob> <days>: delete matching files older than <days> days (never the newest one).
prune() {
    local dir="$1" pattern="$2" days="$3" newest
    [[ -d "${dir}" ]] || return 0
    newest="$(find "${dir}" -maxdepth 1 -type f -name "${pattern}" -printf '%T@ %p\n' | sort -n | tail -1 | cut -d' ' -f2-)"
    find "${dir}" -maxdepth 1 -type f -name "${pattern}" -mtime "+${days}" ! -path "${newest}" -print -delete
}
