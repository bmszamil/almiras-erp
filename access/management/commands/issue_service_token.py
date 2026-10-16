from django.core.management.base import BaseCommand, CommandError

from access.api_auth import issue_service_token
from access.models import User


class Command(BaseCommand):
    help = "Issue a revocable API token for a service user (agent or integration). Printed once."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--label", default="")

    def handle(self, username, label, **opts):
        user = User.objects.filter(username=username, is_active=True).first()
        if user is None or user.is_human:
            raise CommandError("Active service user not found.")
        self.stdout.write(issue_service_token(user, label=label))
