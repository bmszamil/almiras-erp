import csv
import sys

from django.core.management.base import BaseCommand, CommandError

from access import services
from access.models import User


class Command(BaseCommand):
    help = "Quarterly access review: CSV of user x entity x role with last login and 2FA, as seen by --as user."

    def add_arguments(self, parser):
        parser.add_argument("--as", dest="viewer", required=True, help="Username of the reviewer (audit view rights)")

    def handle(self, viewer, **opts):
        user = User.objects.filter(username=viewer, is_active=True).first()
        if user is None:
            raise CommandError("Reviewer not found.")
        rows = services.access_review_report(user)
        fields = ["entity", "username", "user_type", "role", "valid_from", "valid_to", "revoked_at", "granted_by",
                  "active", "user_active", "last_login", "two_factor"]
        w = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
