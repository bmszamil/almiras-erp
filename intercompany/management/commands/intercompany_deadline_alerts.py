"""Daily: alert when an intercompany invoice for month M is still not issued after the 15th of M+1 (D13)."""
from django.core.management.base import BaseCommand

from intercompany import services


class Command(BaseCommand):
    help = "Send ZATCA 15-day issue alerts for intercompany invoices (once per invoice)."

    def handle(self, *args, **opts):
        alerted = services.deadline_alerts()
        self.stdout.write(self.style.SUCCESS(f"{len(alerted)} invoice(s) alerted."))
