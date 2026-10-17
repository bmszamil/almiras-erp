from django.core.management.base import BaseCommand, CommandError

from access import services
from access.errors import BilingualError
from access.models import User


class Command(BaseCommand):
    help = "Create an authenticator-app (TOTP) device for a human user and print its otpauth:// URL (D14). Audited."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="Authenticator app")

    def handle(self, username, name, **opts):
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError("User not found.")
        try:
            device = services.enroll_totp(None, user, name=name)
        except BilingualError as exc:
            raise CommandError(str(exc))
        self.stdout.write("Scan this URL as a QR code in the authenticator app (shown once):")
        self.stdout.write(device.config_url)
