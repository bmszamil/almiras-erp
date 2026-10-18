"""``manage.py migration_validate --entity ALM --user u [--journal f.xlsx] [--tb tb.csv --tb-columns cols.json |
--workbook wb.xlsx] [--cutover YYYY-MM-DD] [--basis closing|opening] [--report out.csv]`` (migration R4 to R11,
W13). Read-only.

Prints the validation report; with both a journal and a trial balance it includes the tie-out (opening + movements =
closing). Exit status 1 when the report has an error. Write ``--report`` outside git (``exports/``).
"""
from django.core.management.base import BaseCommand, CommandError

from migration import report as rendering
from migration import services

from ._common import add_common, add_workbook, cutover, iso_date, read_journal, read_tb, resolve, run


class Command(BaseCommand):
    help = "Validate legacy exports and print the report (writes nothing). / فحص ملفات النظام السابق."

    def add_arguments(self, parser):
        add_common(parser, apply=False)
        parser.add_argument("--journal", help="Falcon general-journal export (xlsx)")
        parser.add_argument("--tb", help="Trial balance export (csv or xlsx)")
        parser.add_argument("--tb-columns", help="Column map JSON for the trial balance")
        add_workbook(parser)
        parser.add_argument("--cutover", type=iso_date, help="Cut-over date (YYYY-MM-DD)")
        parser.add_argument("--basis", choices=["closing", "opening"], default="closing")
        parser.add_argument("--report", help="Write the full report as CSV here")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        if not o["journal"] and not o["tb"] and not o["workbook"]:
            raise CommandError("Give --journal and/or --tb or --workbook. / حدد ملف دفتر اليومية أو ميزان المراجعة.")
        o["cutover"] = cutover(o, entity)
        if o["workbook"]:
            o["basis"] = "closing"  # the workbook's TB is the closing balance at the cut-over (W13)
        journal, tb = run(lambda: read_journal(o)), run(lambda: read_tb(o, entity))
        reports = []
        if journal is not None:
            reports.append(run(lambda: services.validate_journal(entity, journal, user=user, trial_balance=tb,
                                                                 cutover=o["cutover"], source_system=o["source_system"])))
        if tb is not None:
            reports.append(run(lambda: services.validate_trial_balance(entity, tb, user=user, cutover=o["cutover"],
                                                                       basis=o["basis"],
                                                                       source_system=o["source_system"])))
        for r in reports:
            for line in rendering.render(r):
                self.stdout.write(line)
        if o["report"]:
            for i, r in enumerate(reports):
                path = o["report"] if i == 0 else o["report"].replace(".csv", "") + f"-{r.kind}.csv"
                rendering.write_csv(r, path)
                self.stdout.write(f"report written / تم حفظ التقرير: {path}")
        if any(r.errors for r in reports):
            raise CommandError("The report has errors; nothing can be loaded. / في التقرير أخطاء؛ لا يمكن التحميل.")
