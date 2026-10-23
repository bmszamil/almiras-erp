"""``manage.py fixed_assets_setup --entity NEW --user u`` (fixed-assets R31): give an entity its fixed-asset settings
and the group template's categories as data, through ``fixed_assets.services.setup_entity`` (right
``fixed_assets.maintain_setup`` of the named person in that entity). A category whose cost account is not in the
entity's chart yet (a proposed account, OQ-5) is listed and skipped; nothing falls back to another account (R45)."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from core.access import AccessDenied
from core.models import Entity
from fixed_assets import services


class Command(BaseCommand):
    help = "Set up an entity's fixed-asset categories and settings. / إعداد فئات الأصول الثابتة وإعداداتها للكيان."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True, help="Entity code / رمز الكيان")
        parser.add_argument("--user", required=True, help="Username of the person doing this / اسم المستخدم")

    def handle(self, *args, **o):
        entity = Entity.objects.filter(code=o["entity"]).first()
        user = get_user_model().objects.filter(username=o["user"], is_active=True).first()
        if entity is None or user is None:
            raise CommandError("Unknown entity or user. / الكيان أو المستخدم غير معروف.")
        try:
            created, skipped = services.setup_entity(entity, user=user)
        except (AccessDenied, ValidationError) as exc:
            raise CommandError(str(exc)) from None
        for code in created:
            self.stdout.write(f"category {code}: created / أُنشئت")
        for code, account in skipped:
            self.stdout.write(f"category {code}: skipped, account {account} not in the chart yet / تُركت، الحساب "
                              f"{account} غير موجود في الدليل بعد")
