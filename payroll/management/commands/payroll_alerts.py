from django.core.management.base import BaseCommand

from payroll import services


class Command(BaseCommand):
    help = "Send payroll alerts (Mudad, GOSI registration and payment) and nominate old WPS files (daily). / تنبيهات الرواتب."

    def handle(self, **options):
        self.stdout.write(f"{services.send_alerts()} alert(s) / تنبيه")
