"""Apply or check the database-role rules of access.md R45 from inside the application image (no psql needed).

    python /opt/almiras/bin/dbrole.py grants   # as erp_owner, after migrate: sql/grants.sql
    python /opt/almiras/bin/dbrole.py verify   # as erp_app: sql/verify_app_role.sql (AT-47), always rolled back

Connection settings are the ones Django uses (POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST,
POSTGRES_PORT). Exit code 0 = applied / all checks passed; 1 = a check failed or an error occurred.
"""
import os
import sys
from pathlib import Path

import psycopg

SQL_DIR = Path(os.environ.get("ALMIRAS_SQL_DIR", "/opt/almiras/sql"))
FILES = {"grants": "grants.sql", "verify": "verify_app_role.sql"}
EXPECTED_USER = {"grants": "erp_owner", "verify": "erp_app"}


def main(argv):
    if len(argv) != 2 or argv[1] not in FILES:
        print(__doc__, file=sys.stderr)
        return 2
    action = argv[1]
    if not os.environ.get("POSTGRES_DB"):
        print("dbrole: POSTGRES_DB is not set (PostgreSQL only)", file=sys.stderr)
        return 1
    sql = (SQL_DIR / FILES[action]).read_text(encoding="utf-8")
    try:
        with psycopg.connect(
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ.get("POSTGRES_USER", ""),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=os.environ.get("POSTGRES_PORT", "5432"),
            application_name=f"almiras-dbrole-{action}",
        ) as conn:
            conn.add_notice_handler(lambda diag: print(f"{action}: {diag.message_primary}"))
            user = conn.execute("SELECT current_user").fetchone()[0]
            if user != EXPECTED_USER[action]:
                print(f"dbrole {action}: connected as {user}, expected {EXPECTED_USER[action]}", file=sys.stderr)
                return 1
            conn.execute(sql)
            if action == "verify":
                conn.rollback()
            else:
                conn.commit()
    except psycopg.Error as exc:
        print(f"dbrole {action}: FAILED: {exc}".strip(), file=sys.stderr)
        return 1
    print(f"dbrole {action}: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
