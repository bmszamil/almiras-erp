from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = "Activate started employees and assignments; terminate employees past their last day (daily)."

    def handle(self, *args, **opts):
        self.stdout.write(f"{services.refresh_statuses()} status change(s).")
