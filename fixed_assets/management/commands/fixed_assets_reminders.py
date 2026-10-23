"""``manage.py fixed_assets_reminders [--date YYYY-MM-DD]`` (fixed-assets R16, R19): the scheduled job that raises
the year-end estimate-review and reporting-date impairment-indicator reminders (``fixed_assets.jobs``). Run daily;
idempotent. It deletes nothing (R42)."""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from fixed_assets import jobs


class Command(BaseCommand):
    help = "Raise the fixed-asset review reminders due on a date. / إنشاء تذكيرات مراجعة الأصول الثابتة."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Date (default: today in Riyadh) / التاريخ")

    def handle(self, *args, **o):
        try:
            on = date.fromisoformat(o["date"]) if o.get("date") else timezone.localdate()
        except ValueError:
            raise CommandError(f"{o['date']!r} is not a date (YYYY-MM-DD). / ليس تاريخًا.") from None
        for row in jobs.raise_reminders(on):
            self.stdout.write(f"{row.entity.code} {row.kind} {row.due_date}: {row.message_en} / {row.message_ar}")
