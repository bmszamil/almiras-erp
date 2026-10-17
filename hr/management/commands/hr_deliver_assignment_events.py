from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = ("Deliver pending secondment end events to billing (hr R25), every 5 minutes. / تسليم أحداث نهاية "
            "الإعارة المعلقة إلى الفوترة كل 5 دقائق.")

    def handle(self, *args, **opts):
        n = services.deliver_assignment_events()
        self.stdout.write(f"{n} event(s) delivered. / سُلّم {n} حدث.")
