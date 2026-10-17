from django.core.management.base import BaseCommand

from access import services


class Command(BaseCommand):
    help = "Expire due D21 dual-control requests for Chairman accounts (run from cron, e.g. hourly)."

    def handle(self, *args, **opts):
        count = services.expire_chairman_changes()
        self.stdout.write(self.style.SUCCESS(f"{count} request(s) expired."))
