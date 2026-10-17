"""ZATCA 24-hour reporting-deadline watchdog (zatca R-23, R-35, R-47) for monitoring. Read-only.

    python /opt/almiras/bin/zatca_deadline_check.py

Counts, over every entity, stamped simplified e-invoices not yet reported (same filter as
``zatca.services._deadline_checks``) and the age of documents waiting in the queue (held ones excluded), then:

- writes Prometheus textfile metrics (``$ALMIRAS_METRICS_DIR/zatca_deadlines.prom``),
- exits 0 = nothing at risk, 1 = at least one deadline within ``ZATCA_DEADLINE_ALERT_HOURS``,
  2 = at least one deadline already passed (R-35 failure notice due), 3 = queue stuck (a document has waited
  longer than ``ALMIRAS_ZATCA_QUEUE_STUCK_MINUTES``, default 30), 4 = the check itself failed.

The ERP's own job (``zatca_daily`` / ``zatca_process_queue``) still sends the in-app alerts and opens the
late-reporting task; this script only makes the same condition visible to the host monitoring, so a stopped
scheduler or a sealed secret store cannot hide it. It prints counts only, never document data.
"""
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, os.environ.get("ALMIRAS_APP_DIR", "/app"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def write_metrics(values):
    metrics_dir = Path(os.environ.get("ALMIRAS_METRICS_DIR", "/var/lib/almiras/metrics"))
    # Same rule as run-job.sh: no writable metrics directory (e.g. a one-off `docker compose run web`) means no
    # textfile; the exit code still carries the result.
    if not metrics_dir.is_dir() or not os.access(metrics_dir, os.W_OK):
        return
    lines = []
    for name, (help_text, value) in values.items():
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value}"]
    tmp = metrics_dir / ".zatca_deadlines.prom.tmp"
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(metrics_dir / "zatca_deadlines.prom")


def main():
    try:
        import django

        django.setup()
        from django.conf import settings
        from django.utils import timezone

        from zatca.models import EInvoice
        from zatca.services import REPORTED

        now = timezone.now()
        alert_hours = float(getattr(settings, "ZATCA_DEADLINE_ALERT_HOURS", 2))
        stuck_minutes = float(os.environ.get("ALMIRAS_ZATCA_QUEUE_STUCK_MINUTES", "30"))
        Status = EInvoice.Status
        open_simplified = EInvoice.objects.filter(
            zatca_category=EInvoice.Category.SIMPLIFIED, icv__isnull=False
        ).exclude(status__in=(*REPORTED, Status.REJECTED))
        overdue = open_simplified.filter(reporting_deadline__lte=now).count()
        at_risk = open_simplified.filter(reporting_deadline__gt=now,
                                         reporting_deadline__lte=now + timedelta(hours=alert_hours)).count()
        in_queue = EInvoice.objects.filter(status__in=(Status.PENDING, Status.STAMPED, Status.QUEUED,
                                                       Status.SUBMITTING, Status.FAILED_RETRYABLE))
        # Held documents (entity not live, no EGS unit, local validation) wait by design; zatca alerts on them.
        waiting = in_queue.filter(hold_reason="")
        held = in_queue.exclude(hold_reason="").count()
        oldest = waiting.order_by("created_at").values_list("created_at", flat=True).first()
        oldest_age = (now - oldest).total_seconds() if oldest else 0
        waiting_count = waiting.count()
    except Exception as exc:  # noqa: BLE001 - any failure must reach monitoring as exit code 4
        print(f"zatca_deadline_check: check failed: {type(exc).__name__}", file=sys.stderr)
        write_metrics({"almiras_zatca_deadline_check_ok": ("1 if the last check ran.", 0),
                       "almiras_zatca_deadline_check_timestamp_seconds": ("Last check.", int(time.time()))})
        return 4

    stuck = oldest_age > stuck_minutes * 60
    write_metrics({
        "almiras_zatca_simplified_overdue": ("Stamped simplified invoices past the 24 h reporting deadline.", overdue),
        "almiras_zatca_simplified_at_risk": ("Simplified invoices within the alert window of their deadline.", at_risk),
        "almiras_zatca_queue_waiting": ("E-invoices waiting to be stamped, sent or retried.", waiting_count),
        "almiras_zatca_queue_oldest_age_seconds": ("Age of the oldest waiting e-invoice.", int(oldest_age)),
        "almiras_zatca_held": ("E-invoices held (not live, no EGS unit, local validation failed).", held),
        "almiras_zatca_deadline_check_ok": ("1 if the last check ran.", 1),
        "almiras_zatca_deadline_check_timestamp_seconds": ("Last check.", int(time.time())),
    })
    print(f"overdue={overdue} at_risk={at_risk} waiting={waiting_count} held={held} oldest_age_s={int(oldest_age)}")
    if overdue:
        return 2
    if stuck:
        return 3
    if at_risk:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
