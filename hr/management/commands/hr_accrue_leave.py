from datetime import date, timedelta

from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = ("Accrue annual leave for a month (hr R30), idempotent. Default: the previous month. / استحقاق "
            "الإجازة السنوية لشهر، دون تكرار. الافتراضي: الشهر السابق.")

    def add_arguments(self, parser):
        parser.add_argument("--month", help="YYYY-MM")

    def handle(self, *args, month=None, **opts):
        if month:
            year, mon = (int(x) for x in month.split("-"))
        else:
            last = date.today().replace(day=1) - timedelta(days=1)
            year, mon = last.year, last.month
        report = services.accrue_leave(year, mon)
        self.stdout.write(f"{report['accrued']} accrual(s) written. / كُتب {report['accrued']} استحقاق.")
        for code, why in report["skipped"]:
            self.stdout.write(f"Skipped / تخطي {code}: {why}")
