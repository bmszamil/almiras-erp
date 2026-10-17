"""``manage.py migration_tieout --entity ALM --user u --tb tb.csv [--tb-columns cols.json] --as-of YYYY-MM-DD
[--posted-only] [--pl-to-opening-equity] [--report out.csv]`` (migration R18). Read-only.

The ERP trial balance per ERP account against the mapped legacy closing balances, to the halala. Exit status 1 when
there is a difference. This is the tie-out report the accountant signs off before (and after) posting.
"""
import csv

from django.core.management.base import BaseCommand, CommandError

from migration import services

from ._common import add_common, iso_date, read_tb, resolve, run


class Command(BaseCommand):
    help = "Tie the ERP trial balance out to the legacy trial balance (writes nothing). / مطابقة ميزان المراجعة."

    def add_arguments(self, parser):
        add_common(parser, apply=False)
        parser.add_argument("--tb", required=True)
        parser.add_argument("--tb-columns")
        parser.add_argument("--as-of", type=iso_date, required=True)
        parser.add_argument("--posted-only", action="store_true", help="Leave import drafts out")
        parser.add_argument("--pl-to-opening-equity", action="store_true")
        parser.add_argument("--report")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        tb = run(lambda: read_tb(o))
        rows, problems = run(lambda: services.tie_out_ledger(
            entity, tb, user=user, as_of=o["as_of"], include_drafts=not o["posted_only"],
            pl_to_opening_equity=o["pl_to_opening_equity"], source_system=o["source_system"]))
        for r in rows:
            self.stdout.write(f"  {r['erp_code']} expected={r['expected']} ledger={r['actual']} difference={r['difference']}")
        for p in problems:
            self.stdout.write(f"  [{p.severity}] {p.message}")
        if o["report"]:
            with open(o["report"], "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=["erp_code", "expected", "actual", "difference"])
                writer.writeheader()
                writer.writerows(rows)
        if problems:
            raise CommandError(f"{len(problems)} difference(s). / {len(problems)} فرق.")
        self.stdout.write("ties out to the halala / مطابق حتى الهللة")
