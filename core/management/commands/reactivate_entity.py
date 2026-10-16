import getpass

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.models import Entity
from core.services import reactivate_entity_by_operator


class Command(BaseCommand):
    help = (
        "Recovery: reactivate an inactive entity when nobody can do it through core.services.change_entity_settings "
        "(access.md R32). Logged as entity_settings_changed with the operator's OS user. Run by an operator only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="Code of the entity, e.g. ALM")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **opts):
        try:
            entity = Entity.objects.get(code=opts["entity"])
        except Entity.DoesNotExist as exc:
            raise CommandError(str(exc)) from exc
        try:
            reactivate_entity_by_operator(entity, opts["reason"], os_user=getpass.getuser())
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(f"Reactivated {entity.code}."))
