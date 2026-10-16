import os
import pwd

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.models import Entity
from core.services import reactivate_entity_by_operator


class Command(BaseCommand):
    help = (
        "Recovery (access.md R32, R39): reactivate an inactive entity that nobody can reactivate through "
        "core.services.change_entity_settings, i.e. an inactive top-level entity (no parent) or an inactive entity "
        "whose group parent is also inactive. An inactive subsidiary of an active group is refused: its group's "
        "managing partner reactivates it. Logged as entity_settings_changed with the operator's OS user (from the "
        "process uid, not the environment). Run by an operator only."
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
            reactivate_entity_by_operator(entity, opts["reason"], os_user=pwd.getpwuid(os.getuid()).pw_name)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(f"Reactivated {entity.code}."))
