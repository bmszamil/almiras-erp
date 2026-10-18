"""``manage.py migration_load --entity ALM --user u --cutover YYYY-MM-DD [--tb tb.csv --tb-columns cols.json |
--workbook wb.xlsx] [--basis closing|opening] [--pl-to-opening-equity] [--journal f.xlsx] [--apply]``
(migration R14, R15, W13). With ``--workbook`` the cut-over date defaults to the entity's date on its Entities sheet
and the opening entry is drafted from its closing balances there.

Drafts the opening entry from the trial balance and/or replays the journal's entries as drafts, through
ledger.services only. Dry run unless ``--apply``; nothing is created when the report has an error. The drafts are
posted later by a second person with ``migration_post``.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from migration import report as rendering
from migration import services

from ._common import add_common, add_workbook, cutover, iso_date, read_journal, read_tb, resolve, run


class Command(BaseCommand):
    help = "Draft opening balances and replayed journals from legacy exports (dry run unless --apply)."

    def add_arguments(self, parser):
        add_common(parser)
        parser.add_argument("--cutover", type=iso_date, help="Cut-over date (YYYY-MM-DD); required without --workbook")
        parser.add_argument("--tb", help="Trial balance export: load the opening entry from it")
        parser.add_argument("--tb-columns", help="Column map JSON for the trial balance")
        add_workbook(parser)
        parser.add_argument("--basis", choices=["closing", "opening"], default=None,
                            help="Balances to load (default closing; opening when --journal is given)")
        parser.add_argument("--pl-to-opening-equity", action="store_true",
                            help="Net income and expense balances into 3900 (OQ-3)")
        parser.add_argument("--journal", help="Falcon journal export: replay its entries after the cut-over")
        parser.add_argument("--report", help="Write the report(s) as CSV here")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        if not o["tb"] and not o["journal"] and not o["workbook"]:
            raise CommandError("Give --tb, --workbook and/or --journal. / حدد ميزان المراجعة أو دفتر اليومية.")
        o["cutover"] = cutover(o, entity)
        if o["cutover"] is None:
            raise CommandError("Give --cutover (YYYY-MM-DD). / حدد تاريخ التحويل.")
        if o["workbook"]:
            if o["basis"] == "opening":
                raise CommandError("The workbook holds closing balances at the cut-over (--basis closing). / "
                                   "يحتوي الملف على الأرصدة الختامية في تاريخ التحويل.")
            basis = "closing"
        else:
            basis = o["basis"] or ("opening" if o["journal"] else "closing")
            if o["journal"] and o["tb"] and basis != "opening":
                raise CommandError("With --journal the trial balance loads its opening balances (--basis opening). / "
                                   "مع دفتر اليومية تُحمَّل الأرصدة الافتتاحية من الميزان.")
        journal, tb = run(lambda: read_journal(o)), run(lambda: read_tb(o, entity))
        journal_tb = None if o["workbook"] else tb  # the workbook's TB is at the cut-over: no movement tie-out
        plans = []
        with transaction.atomic():
            if tb is not None:
                plans.append(run(lambda: services.load_opening_balances(
                    entity, tb, user=user, cutover=o["cutover"], basis=basis,
                    pl_to_opening_equity=o["pl_to_opening_equity"], apply=o["apply"],
                    source_system=o["source_system"])))
            if journal is not None and not (plans and plans[0].report.errors):
                plans.append(run(lambda: services.load_journal_entries(
                    entity, journal, user=user, cutover=o["cutover"], trial_balance=journal_tb, apply=o["apply"],
                    source_system=o["source_system"])))
            failed = any(p.report.errors for p in plans)
            if failed:
                transaction.set_rollback(True)
        for i, plan in enumerate(plans):
            for line in rendering.render(plan.report):
                self.stdout.write(line)
            mode = "created / أُنشئت" if plan.applied else "would create (dry run) / سيُنشأ (تجربة)"
            self.stdout.write(f"{mode}: {len(plan.entries)} draft(s); skipped (already imported): {len(plan.skipped)}")
            for e in plan.entries[:50]:
                self.stdout.write(f"  {e['legacy_key']} {e['date']} lines={e['lines']} debit={e['debit']}")
            if o["report"]:
                path = o["report"] if i == 0 else o["report"].replace(".csv", "") + f"-{plan.report.kind}.csv"
                rendering.write_csv(plan.report, path)
        if failed:
            raise CommandError("The report has errors; nothing was created. / في التقرير أخطاء؛ لم يُنشأ شيء.")
