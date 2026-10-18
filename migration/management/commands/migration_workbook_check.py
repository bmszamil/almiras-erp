"""``manage.py migration_workbook_check --workbook exports/wb.xlsx [--user u] [--entity E ...] [--report out.csv]``
(migration.md §11, W12). Read-only.

Checks a filled migration workbook: required values and types, balancing trial balances, mapping completeness and
the sub-ledger tie-outs (open items, banks, end of service, fixed assets) against the trial balance. With ``--user``
the ERP accounts are checked against each entity's chart in the ERP (right ``migration.view``); entities not set up
yet, and every entity without ``--user``, against the group chart template. Exit status 1 when the report has an
error. The workbook and the CSV report stay outside git (``exports/``).
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core import access
from core.models import Entity
from migration import workbook, workbook_checks

from ._common import run


class Command(BaseCommand):
    help = "Check a filled migration workbook (writes nothing). / فحص ملف الترحيل المعبأ."

    def add_arguments(self, parser):
        parser.add_argument("--workbook", required=True, help="Filled workbook (xlsx)")
        parser.add_argument("--user", help="Username: check ERP accounts against each entity's chart in the ERP")
        parser.add_argument("--entity", action="append", default=[], help="Check only this entity (repeatable)")
        parser.add_argument("--report", help="Write the report as CSV here (outside git)")

    def handle(self, *args, **o):
        book = run(lambda: workbook.read_workbook(o["workbook"]))
        only = [code.upper() for code in o["entity"]] or None
        charts, notes = {}, []
        if o["user"]:
            user = get_user_model().objects.filter(username=o["user"], is_active=True).first()
            if user is None:
                raise CommandError(f"Unknown or inactive user {o['user']}. / المستخدم {o['user']} غير معروف أو غير نشط.")
            for code in only or book.entity_codes():
                entity = Entity.objects.filter(code=code).first()
                if entity is None:
                    notes.append(f"{code}: not set up in the ERP yet, checked against the group template / "
                                 f"غير مُعد في النظام بعد، فُحص مقابل القالب العام")
                    continue
                run(lambda entity=entity: access.require(user, entity, "migration.view"))
                charts[code] = workbook_checks.entity_chart(entity)
        report = workbook_checks.check_workbook(book, charts=charts, entities=only)
        for line in notes + workbook_checks.render(report):
            self.stdout.write(line)
        if o["report"]:
            workbook_checks.write_csv(report, o["report"])
            self.stdout.write(f"report written / تم حفظ التقرير: {o['report']}")
        if report.errors:
            raise CommandError(f"The workbook has {len(report.errors)} error(s); fix them before loading. / "
                               f"في الملف {len(report.errors)} خطأ؛ صححها قبل التحميل.")
        self.stdout.write("no errors / لا أخطاء")
