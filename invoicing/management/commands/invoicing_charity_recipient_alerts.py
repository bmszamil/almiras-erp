from django.core.management.base import BaseCommand

from invoicing import services


class Command(BaseCommand):
    help = ("Alert charity recipient approvals expiring in 30 and 7 days (owner decision C3), daily, idempotent. / "
            "التنبيه بانتهاء اعتماد الجهات الخيرية بعد 30 و7 أيام يوميًا.")

    def handle(self, *args, **opts):
        n = services.send_charity_recipient_alerts()
        self.stdout.write(f"{n} charity recipient(s) alerted. / نُبّه عن {n} جهة خيرية.")
