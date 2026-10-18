"""``manage.py migration_template [--output PATH]`` (migration.md §11, W1).

Writes the fillable migration workbook from ``migration/workbook_spec.py`` (default
``docs/migration/almiras-migration-workbook.xlsx``). Run it after changing the spec and commit the regenerated file;
the committed template never holds real data.
"""
from pathlib import Path

from django.core.management.base import BaseCommand

from migration import workbook


class Command(BaseCommand):
    help = "Write the fillable migration workbook template. / إنشاء قالب ملف الترحيل."

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(workbook.DEFAULT_PATH), help="Where to write the xlsx")

    def handle(self, *args, **o):
        path = Path(o["output"])
        path.parent.mkdir(parents=True, exist_ok=True)
        workbook.build_workbook(path)
        self.stdout.write(f"template written / تم إنشاء القالب: {path}")
