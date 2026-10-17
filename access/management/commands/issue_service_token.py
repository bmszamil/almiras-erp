import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from access import services
from access.errors import BilingualError
from access.models import User


class Command(BaseCommand):
    help = "Issue a revocable, expiring API token for a service user (agent or integration). Printed once; audited."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--days", type=int, required=True, help="Token lifetime in days (mandatory)")
        parser.add_argument("--label", default="")

    def handle(self, username, days, label, **opts):
        user = User.objects.filter(username=username, is_active=True).first()
        if user is None or user.is_human:
            raise CommandError("Active service user not found.")
        if days < 1:
            raise CommandError("--days must be at least 1.")
        try:
            key = services.issue_service_token(None, user, label=label,
                                               expires_at=timezone.now() + datetime.timedelta(days=days))
        except BilingualError as exc:
            raise CommandError(str(exc))
        self.stdout.write(key)
