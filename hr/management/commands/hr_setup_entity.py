from django.core.management.base import BaseCommand, CommandError

from core.models import Entity
from hr import services


class Command(BaseCommand):
    help = ("Set up HR for an entity (hr R4): settings, seeded salary components and leave types. Safe to re-run. / "
            "إعداد الموارد البشرية لكيان: الإعدادات ومكوّنات الرواتب وأنواع الإجازات. آمن عند إعادة التشغيل.")

    def add_arguments(self, parser):
        parser.add_argument("code")

    def handle(self, *args, code, **opts):
        entity = Entity.objects.filter(code=code).first()
        if entity is None:
            raise CommandError(f"No entity {code}. / لا يوجد كيان {code}.")
        s = services.setup_entity(entity)
        self.stdout.write(self.style.SUCCESS(f"HR set up for {entity.code} ({s.jurisdiction}, overtime route "
                                             f"{s.overtime_approval_route}). / أُعدّت الموارد البشرية للكيان {entity.code}."))
