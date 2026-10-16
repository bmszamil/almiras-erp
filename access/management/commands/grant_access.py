from django.core.management.base import BaseCommand, CommandError

from access import services
from access.models import Role, User
from core.models import Entity


class Command(BaseCommand):
    help = "Bootstrap: grant a role in an entity to a user (audited as the system user). Use the API/admin afterwards."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("entity_code")
        parser.add_argument("role_code")

    def handle(self, username, entity_code, role_code, **opts):
        try:
            user = User.objects.get(username=username)
            entity = Entity.objects.get(code=entity_code)
            role = Role.objects.get(code=role_code)
        except (User.DoesNotExist, Entity.DoesNotExist, Role.DoesNotExist) as exc:
            raise CommandError(str(exc))
        grant = services.grant_access(None, user, entity, role)
        self.stdout.write(self.style.SUCCESS(f"Granted {grant}."))
