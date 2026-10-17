"""Container health checks (stdlib only).

    python /opt/almiras/bin/healthcheck.py            # web: GET /admin/login/ on gunicorn answers 200
    python /opt/almiras/bin/healthcheck.py scheduler  # scheduler: the every-minute ZATCA queue job ran recently

Exit 0 = healthy, 1 = unhealthy.
"""
import http.client
import os
import sys
import time
from pathlib import Path


def web():
    host = os.environ.get("ALMIRAS_HEALTHCHECK_HOST", "localhost")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 8000, timeout=4)
        conn.request("GET", "/admin/login/", headers={"Host": host, "X-Forwarded-Proto": "https"})
        status = conn.getresponse().status
    except OSError as exc:
        print(f"web unhealthy: {exc}")
        return 1
    if status != 200:
        print(f"web unhealthy: HTTP {status}")
        return 1
    return 0


def scheduler():
    metrics = Path(os.environ.get("ALMIRAS_METRICS_DIR", "/var/lib/almiras/metrics"))
    heartbeat = metrics / "job_zatca_process_queue.prom"
    max_age = int(os.environ.get("ALMIRAS_SCHEDULER_MAX_SILENCE_SECONDS", "600"))
    try:
        age = time.time() - heartbeat.stat().st_mtime
    except FileNotFoundError:
        # Not yet written right after start (catch-up jobs run first): healthy during the start period.
        return 0 if time.time() - Path("/proc/1").stat().st_mtime < max_age else 1
    if age > max_age:
        print(f"scheduler unhealthy: zatca_process_queue last finished {int(age)} s ago")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(scheduler() if sys.argv[1:] == ["scheduler"] else web())
