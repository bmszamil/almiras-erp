#!/bin/sh
# Wrapper for every scheduled job (scheduler/crontab):
#   run-job.sh <name> [--timeout SECONDS] -- <command> [args...]
# - one run at a time per job (flock on /tmp/almiras-job-<name>.lock; an overlapping start is skipped and logged),
# - a hard timeout (default 3300 s),
# - one JSON log line at the start and at the end (stdout -> docker logs -> journald on the KSA host),
# - Prometheus textfile metrics in $ALMIRAS_METRICS_DIR/job_<name>.prom (last run, last success, exit code,
#   duration), which monitoring/alert-rules.yml turns into alerts,
# - on failure, an optional POST to ALMIRAS_ALERT_WEBHOOK_URL (a KSA-hosted endpoint such as the local
#   Alertmanager or the company chat server; never a foreign SaaS). The body carries the job name and exit
#   code only, no command output (which may hold personal data).
# Exit code = the job's exit code (124 = timeout, 75 = skipped because the previous run is still going).
set -u

name="$1"
shift
timeout_s=3300
if [ "${1:-}" = "--timeout" ]; then
    timeout_s="$2"
    shift 2
fi
if [ "${1:-}" = "--" ]; then
    shift
fi

metrics_dir="${ALMIRAS_METRICS_DIR:-/var/lib/almiras/metrics}"
lock="/tmp/almiras-job-${name}.lock"
started="$(date +%s)"

log() {
    printf '{"ts":"%s","job":"%s","event":"%s"%s}\n' "$(date --iso-8601=seconds)" "${name}" "$1" "${2:-}"
}

write_metrics() {
    code="$1"
    [ -d "${metrics_dir}" ] && [ -w "${metrics_dir}" ] || return 0
    file="${metrics_dir}/job_${name}.prom"
    tmp="${metrics_dir}/.job_${name}.prom.$$"
    now="$(date +%s)"
    last_success=""
    if [ "${code}" = "0" ]; then
        last_success="${now}"
    elif [ -f "${file}" ]; then
        last_success="$(sed -n 's/^almiras_job_last_success_timestamp_seconds{[^}]*} //p' "${file}")"
    fi
    {
        echo "# HELP almiras_job_last_run_timestamp_seconds When the scheduled job last finished."
        echo "# TYPE almiras_job_last_run_timestamp_seconds gauge"
        echo "almiras_job_last_run_timestamp_seconds{task=\"${name}\"} ${now}"
        echo "# HELP almiras_job_last_exit_code Exit code of the last run (0 = success)."
        echo "# TYPE almiras_job_last_exit_code gauge"
        echo "almiras_job_last_exit_code{task=\"${name}\"} ${code}"
        echo "# HELP almiras_job_last_duration_seconds Duration of the last run."
        echo "# TYPE almiras_job_last_duration_seconds gauge"
        echo "almiras_job_last_duration_seconds{task=\"${name}\"} $((now - started))"
        if [ -n "${last_success}" ]; then
            echo "# HELP almiras_job_last_success_timestamp_seconds When the job last succeeded."
            echo "# TYPE almiras_job_last_success_timestamp_seconds gauge"
            echo "almiras_job_last_success_timestamp_seconds{task=\"${name}\"} ${last_success}"
        fi
    } > "${tmp}" && mv "${tmp}" "${file}"
}

alert() {
    code="$1"
    url="${ALMIRAS_ALERT_WEBHOOK_URL:-}"
    [ -n "${url}" ] || return 0
    python - "${url}" "${name}" "${code}" <<'PY' || true
import json, sys, urllib.request
url, job, code = sys.argv[1], sys.argv[2], int(sys.argv[3])
body = json.dumps({"source": "almiras-scheduler", "job": job, "exit_code": code,
                   "text_en": f"Scheduled job {job} failed (exit code {code}).",
                   "text_ar": f"فشلت المهمة المجدولة {job} (رمز الخروج {code})."}).encode()
req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
urllib.request.urlopen(req, timeout=10).close()
PY
}

exec 9> "${lock}"
if ! flock --nonblock 9; then
    log "skipped" ',"reason":"previous run still active"'
    exit 75
fi

log "start"
timeout --kill-after=30 "${timeout_s}" "$@"
code=$?
write_metrics "${code}"
if [ "${code}" -eq 0 ]; then
    log "ok" ",\"duration_s\":$(( $(date +%s) - started ))"
else
    log "failed" ",\"exit_code\":${code},\"duration_s\":$(( $(date +%s) - started ))"
    alert "${code}"
fi
exit "${code}"
