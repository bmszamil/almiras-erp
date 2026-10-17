from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = "Send document expiry alerts (hr R43), daily, idempotent."

    def handle(self, *args, **opts):
        self.stdout.write(f"{services.send_document_alerts()} alert(s) sent.")
