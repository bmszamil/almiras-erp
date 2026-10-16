from django.core.management.base import BaseCommand

from approvals.services import run_escalations


class Command(BaseCommand):
    help = "Escalate overdue approval steps and expire timed-out requests (R15). Run every 15 minutes from cron."

    def handle(self, *args, **opts):
        escalated, expired = run_escalations()
        self.stdout.write(f"Escalated {escalated}, expired {expired}.")
