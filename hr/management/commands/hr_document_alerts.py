from django.core.management.base import BaseCommand

from hr import services


class Command(BaseCommand):
    help = ("Send document expiry alerts (hr R43), daily, idempotent. / إرسال تنبيهات انتهاء المستندات يوميًا.")

    def handle(self, *args, **opts):
        n = services.send_document_alerts()
        self.stdout.write(f"{n} alert(s) sent. / أُرسل {n} تنبيه.")
