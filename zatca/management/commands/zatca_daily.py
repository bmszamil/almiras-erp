"""Daily ZATCA job: credential renewal and expiry (R-10) and reporting-deadline checks (R-23, R-35)."""
from django.core.management.base import BaseCommand

from core import access
from zatca import services


class Command(BaseCommand):
    help = "Mark EGS units due for renewal or expired and run the reporting-deadline checks."

    def handle(self, *args, **options):
        queue = access.system_user("zatca_queue")
        changed = services.refresh_credential_statuses(user=queue)
        services.run_deadline_checks(user=queue)
        self.stdout.write(f"egs_units_changed={len(changed)}")
