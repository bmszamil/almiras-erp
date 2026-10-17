from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = ("Activate started employees and assignments; terminate employees past their last day (daily). / "
            "تفعيل الموظفين والتكليفات التي بدأت وإنهاء خدمة من تجاوز آخر يوم عمل (يوميًا).")

    def handle(self, *args, **opts):
        n = services.refresh_statuses()
        self.stdout.write(f"{n} status change(s). / {n} تغيير في الحالة.")
