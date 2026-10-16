from django.core.management.base import BaseCommand

from core.models import Entity
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts


class Command(BaseCommand):
    help = "Create the Almiras parent entity, its chart of accounts and 2027 monthly periods."

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=2027)

    def handle(self, *args, **opts):
        entity, created = Entity.objects.get_or_create(
            code="ALM",
            defaults=dict(
                name_en="Almiras",
                name_ar="المراس",
                consolidation=Entity.Consolidation.PARENT,
                reporting_standard=Entity.Standard.IFRS_SME,
            ),
        )
        accounts = install_chart_of_accounts(entity)
        periods = create_monthly_periods(entity, opts["year"])
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Found'} {entity}; {accounts} accounts and {periods} periods added."
            )
        )
