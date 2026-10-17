#!/bin/sh
# Scheduler container start (entrypoint mode "scheduler").
# supercronic, unlike a systemd timer with Persistent=true (billing.md R66), does not replay runs missed while
# the host or container was down. Every daily job below is idempotent (billing R11, hr R30/R43, zatca R-10), so
# the scheduler runs them once at start-up as a catch-up, in the same order as the nightly schedule, then hands
# over to supercronic. A failed catch-up job is logged and alerted by run-job.sh but does not block the start.
set -u

bin=/opt/almiras/bin
if [ "${ALMIRAS_SCHEDULER_CATCHUP:-1}" = "1" ]; then
    "${bin}/run-job.sh" hr_refresh_status -- python manage.py hr_refresh_status || true
    "${bin}/run-job.sh" billing_run -- python manage.py billing_run || true
    "${bin}/run-job.sh" zatca_daily -- python manage.py zatca_daily || true
fi

exec supercronic -passthrough-logs -overlapping=false "${ALMIRAS_CRONTAB:-/opt/almiras/crontab}"
