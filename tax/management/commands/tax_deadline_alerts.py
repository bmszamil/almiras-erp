from datetime import date

from django.core.management.base import BaseCommand

from tax import jobs


class Command(BaseCommand):
    help = "Write today's VAT deadline alerts (tax-returns R13). / كتابة تنبيهات مواعيد الإقرارات الضريبية."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Run as of this date (YYYY-MM-DD); default today in Riyadh.")

    def handle(self, *args, **options):
        on = date.fromisoformat(options["date"]) if options.get("date") else None
        alerts = jobs.deadline_alerts(today=on)
        self.stdout.write(f"{len(alerts)} alert(s) written.")
