from django.core.management.base import BaseCommand

from core.models import Entity
from payroll import services


class Command(BaseCommand):
    help = "Create the payroll settings of an entity (disabled until configured; R3). / إنشاء إعدادات الرواتب لكيان."

    def add_arguments(self, parser):
        parser.add_argument("code")

    def handle(self, code, **options):
        s = services.setup_entity(Entity.objects.get(code=code))
        self.stdout.write(f"Payroll settings for {s.entity.code}: enabled={s.enabled}")
