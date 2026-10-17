"""Run the ZATCA queue once as the ZATCA queue system user (stamp, submit, retry, results, deadlines)."""
from django.core.management.base import BaseCommand

from core import access
from zatca import services


class Command(BaseCommand):
    help = "Process the ZATCA e-invoicing queue once (zatca R-20 to R-27, R-35). Schedule it every minute."

    def handle(self, *args, **options):
        counts = services.process_queue(user=access.system_user("zatca_queue"))
        self.stdout.write(f"stamped={counts['stamped']} sent={counts['sent']}")
