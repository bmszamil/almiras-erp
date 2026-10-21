from django.core.management.base import BaseCommand, CommandError

from payroll import services


class Command(BaseCommand):
    help = "Send payroll alerts (Mudad, GOSI registration and payment) and nominate old WPS files (daily). / تنبيهات الرواتب."

    def handle(self, **options):
        # Review 4 (2026-10-21, item 2): every entity is processed even when one fails; the command then exits
        # non-zero (CommandError) naming the failed entities and steps, so the scheduler reports it.
        try:
            sent = services.send_alerts()
        except services.AlertsFailed as exc:
            self.stdout.write(f"{exc.sent} alert(s) / تنبيه")
            for code, step, message in exc.failures:
                self.stderr.write(f"{code} {step}: {message}")
            raise CommandError(exc.messages[0], returncode=1) from None
        self.stdout.write(f"{sent} alert(s) / تنبيه")
