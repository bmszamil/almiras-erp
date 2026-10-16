from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Entity
from core.services import ALMIRAS_SETUP_DATA, apply_entity_setup, load_setup_data
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts


class Command(BaseCommand):
    help = (
        "Create or update the Almiras parent entity (registration and national address from "
        "core/setup_data/almiras.json), its chart of accounts and monthly periods. Safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, default=2027)
        parser.add_argument(
            "--setup-data",
            default=str(ALMIRAS_SETUP_DATA),
            help="Path to the entity setup-data JSON file (tests pass an anonymised file).",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        data = load_setup_data(opts["setup_data"])
        entity, created = Entity.objects.get_or_create(
            code=data["code"],
            defaults=dict(
                name_en="Almiras",
                name_ar="المراس",
                consolidation=Entity.Consolidation.PARENT,
                reporting_standard=Entity.Standard.IFRS_SME,
            ),
        )
        apply_entity_setup(entity, data["fields"])
        accounts = install_chart_of_accounts(entity)
        periods = create_monthly_periods(entity, opts["year"])
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Created' if created else 'Updated'} {entity}; {accounts} accounts and {periods} periods added."
            )
        )
