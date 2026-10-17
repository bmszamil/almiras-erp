from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = "Deliver pending secondment end events to billing (hr R25), every 5 minutes."

    def handle(self, *args, **opts):
        self.stdout.write(f"{services.deliver_assignment_events()} event(s) delivered.")
