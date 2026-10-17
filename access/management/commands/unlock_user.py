from django.core.management.base import BaseCommand, CommandError

from access import services
from access.models import User


class Command(BaseCommand):
    help = "Clear the sign-in lockout of a user (audited as access.unlock)."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, username, **opts):
        user = User.objects.filter(username=username).first()
        if user is None:
            raise CommandError("User not found.")
        services.unlock_user(None, user)
        self.stdout.write(self.style.SUCCESS(f"Unlocked {username}."))
