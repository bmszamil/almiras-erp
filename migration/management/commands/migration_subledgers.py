"""``manage.py migration_subledgers --entity ALM --user u --workbook wb.xlsx --section receivables payables ...
[--apply]`` (migration R23, OQ-12; dry run 2026-10-21 gaps 1, 4 and 5).

Loads one entity's sub-ledgers from a filled migration workbook into the modules that own them, through their own
services and rights (``migration.subledgers``): ``receivables`` and ``payables`` (customers, vendors and open items
into invoicing, never sent to ZATCA, no journal entry), ``banks`` (bank account drafts, approved in banking),
``employees`` (draft hr records), ``eosb`` (end-of-service provisions at the cut-over), ``leave`` (opening leave
balances, approved in hr) and ``bai_ajel`` (migrated bai' ajel contracts); ``all`` for every section. The entity's
workbook rows must check without an error. Dry run unless ``--apply``; all or nothing. The opening entry then posts
only when what is loaded ties out to it (R22, ``migration_post``).
"""
from django.core.management.base import BaseCommand, CommandError

from migration import subledgers

from ._common import add_common, read_book, resolve, run


class Command(BaseCommand):
    help = "Load an entity's sub-ledgers from the migration workbook (dry run unless --apply). / تحميل السجلات الفرعية."

    def add_arguments(self, parser):
        add_common(parser)
        parser.add_argument("--workbook", required=True, help="Filled migration workbook (xlsx) / ملف الترحيل المعبأ")
        parser.add_argument("--section", nargs="+", required=True, choices=[*subledgers.SECTIONS, "all"],
                            help="Sections to load / الأقسام")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        sections = list(subledgers.SECTIONS) if "all" in o["section"] else o["section"]
        book = read_book(o)
        plan = run(lambda: subledgers.load_subledgers(entity, book, sections, user=user, apply=o["apply"],
                                                      source_system=o["source_system"]))
        for line in subledgers.render(plan):
            self.stdout.write(line)
        if not plan.ok:
            raise CommandError("Nothing was loaded: fix the workbook errors and refusals above. / لم يُحمَّل شيء: صحّح "
                               "الأخطاء والرفض أعلاه.")
        self.stdout.write("loaded / تم التحميل" if plan.applied else "dry run: nothing kept / تجربة: لم يُحفظ شيء")
