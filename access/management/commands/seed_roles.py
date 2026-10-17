from django.core.management.base import BaseCommand

from access.catalogue import seed
from access.models import Permission, Role


class Command(BaseCommand):
    help = "Create missing ERP permissions and starting roles (idempotent; never removes permissions)."

    def handle(self, *args, **opts):
        perms, roles = seed(Permission, Role)
        self.stdout.write(self.style.SUCCESS(f"{perms} permissions and {roles} system roles present."))
