"""Monthly governance report job (access.md R84): run from 02:00 Riyadh time on the 1st of each month.

Generates, as the ``system.governance`` user, the previous month's report for every active entity where that user
has a membership; entities without it are listed and their top authority is alerted. Re-running is harmless (an
existing report is returned unchanged).
"""
from django.core.management.base import BaseCommand

from core.access import reports


class Command(BaseCommand):
    help = "Generate the monthly governance reports (access R84). / إنشاء تقارير الحوكمة الشهرية."

    def handle(self, *args, **options):
        generated, skipped = reports.run_monthly_reports()
        for report in generated:
            self.stdout.write(f"{report.entity.code} {report.month:%Y-%m} revision {report.revision}")
        for entity in skipped:
            self.stdout.write(f"{entity.code}: skipped, no governance system user membership / لا توجد عضوية")
