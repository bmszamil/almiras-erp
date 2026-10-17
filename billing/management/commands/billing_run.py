"""``python manage.py billing_run [--date YYYY-MM-DD] [--entity CODE]`` (billing.md R66).

Runs ``billing.services.run_billing`` as the billing system user, daily at 00:30 Asia/Riyadh (systemd timer with
``Persistent=true`` or cron). Re-running is always safe (R11). Exit code 0 when every run completed (with or
without errors) or was skipped for lack of a ``system_billing`` membership; non-zero when a run aborted.
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from billing import services
from billing.models import BillingRun
from core import access
from core.models import Entity


class Command(BaseCommand):
    help = "Generate due invoices from approved contracts (billing R66). / إصدار الفواتير المستحقة من العقود المعتمدة."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Run date YYYY-MM-DD (default: today in Asia/Riyadh)")
        parser.add_argument("--entity", help="Entity code (default: every entity the billing system user may bill)")

    def handle(self, *args, **options):
        run_date = date.fromisoformat(options["date"]) if options.get("date") else None
        entity = None
        if options.get("entity"):
            entity = Entity.objects.filter(code=options["entity"]).first()
            if entity is None:
                raise CommandError(f"Unknown entity {options['entity']!r}. / الكيان غير معروف.")
        runs = services.run_billing(run_date, entity=entity, user=access.system_user("billing"))
        for run in runs:
            self.stdout.write(
                f"{run.entity.code} {run.run_date} {run.status}: generated {run.generated_count}, skipped "
                f"{run.skipped_count}, held {run.held_count}, failed {run.failed_count}"
            )
            for m in run.messages:
                self.stdout.write(f"  {m.get('en', '')} / {m.get('ar', '')}")
        if any(run.status == BillingRun.Status.ABORTED for run in runs):
            raise CommandError("A billing run aborted. / توقفت إحدى دورات الفوترة.")
