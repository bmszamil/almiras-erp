import getpass

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.access.services import bootstrap_managing_partner, kind_of
from core.models import Entity


class Command(BaseCommand):
    help = (
        "Grant the first managing_partner membership on the group parent entity (access.md R26). "
        "Refused once any active managing_partner membership exists; logged as a bootstrap event."
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", required=True)
        parser.add_argument("--entity", required=True, help="Code of the group parent entity, e.g. ALM")
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **opts):
        User = get_user_model()
        try:
            user = User.objects.get(username=opts["username"])
            entity = Entity.objects.get(code=opts["entity"])
        except (User.DoesNotExist, Entity.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        if kind_of(user) != "human" or not user.is_active:
            raise CommandError("The managing partner must be an active human user.")
        try:
            m = bootstrap_managing_partner(user, entity, opts["reason"], os_user=getpass.getuser())
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        self.stdout.write(self.style.SUCCESS(f"Granted managing_partner to {user} on {entity.code} (membership {m.pk})."))
