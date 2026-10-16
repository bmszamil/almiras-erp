from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Entity
from core.services import ALMIRAS_CODE, ALMIRAS_SETUP_DATA, apply_entity_setup, load_setup_data
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts


class Command(BaseCommand):
    help = (
        f"Create or update the Almiras parent entity (code {ALMIRAS_CODE}), its chart of accounts and "
        "monthly periods. The setup-data file (core/setup_data/almiras.json) is the source of truth for "
        "registration and national-address fields: re-running overwrites any edits made to those fields "
        "in the admin. The file must be for entity ALM; a file for any other entity is rejected. "
        "Safe to re-run."
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
        # The target entity is fixed here; the file's "code" only has to agree with it.
        data = load_setup_data(opts["setup_data"])
        entity, created = Entity.objects.get_or_create(
            code=ALMIRAS_CODE,
            defaults=dict(
                name_en="Almiras",
                name_ar="المراس",
                consolidation=Entity.Consolidation.PARENT,
                reporting_standard=Entity.Standard.IFRS_SME,
            ),
        )
        changed = apply_entity_setup(entity, data["fields"])
        accounts = install_chart_of_accounts(entity)
        periods = create_monthly_periods(entity, opts["year"])
        if created:
            state = "Created"
        elif changed:
            state = f"Updated ({', '.join(changed)})"
        else:
            state = "Unchanged"
        self.stdout.write(self.style.SUCCESS(f"{state} {entity}; {accounts} accounts and {periods} periods added."))
