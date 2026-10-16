from django.core.management.base import BaseCommand

from approvals.notify import retry_failed_emails


class Command(BaseCommand):
    help = "Retry notification emails that failed or are still pending (R21, T23). Run from cron."

    def handle(self, *args, **opts):
        self.stdout.write(f"Sent {retry_failed_emails()} emails.")
