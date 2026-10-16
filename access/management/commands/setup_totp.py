from django.core.management.base import BaseCommand, CommandError
from django_otp.plugins.otp_totp.models import TOTPDevice

from access.models import User


class Command(BaseCommand):
    help = "Create an authenticator-app (TOTP) device for a human user and print its otpauth:// URL (D14)."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="Authenticator app")

    def handle(self, username, name, **opts):
        user = User.objects.filter(username=username, is_active=True).first()
        if user is None or not user.is_human:
            raise CommandError("Active human user not found.")
        TOTPDevice.objects.filter(user=user, confirmed=True, name=name).delete()
        device = TOTPDevice.objects.create(user=user, name=name, confirmed=True)
        self.stdout.write("Scan this URL as a QR code in the authenticator app (shown once):")
        self.stdout.write(device.config_url)
