"""ZATCA secret-version retirement job (zatca D-19, R-58, R-60): retry pending retirements with the R-24 backoff and
retire unreferenced key and CSID secret versions after ``ZATCA_RETIRE_GRACE_MINUTES``. Nothing is ever destroyed."""
from django.core.management.base import BaseCommand

from core import access
from zatca import services


class Command(BaseCommand):
    help = "Retire superseded ZATCA key and secret versions (zatca R-57 to R-60). Schedule it every minute."

    def handle(self, *args, **options):
        counts = services.run_secret_retirement(user=access.system_user("zatca_queue"))
        self.stdout.write(f"retired={counts['retired']} failed={counts['failed']} errors={counts['errors']}")
