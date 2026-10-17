"""Monthly intercompany billing run for cron: drafts only (R9), idempotent (R4).

    python manage.py run_intercompany_billing --entity ALM --period 2026-11 --as <username>

The run is made as a named human user who holds intercompany.billing.run in the entity.
"""
from django.core.management.base import BaseCommand, CommandError

from access.errors import BilingualError
from access.models import User
from core.models import Entity
from intercompany import services
from ledger.models import FiscalPeriod


class Command(BaseCommand):
    help = "Create draft intercompany invoices for one entity and fiscal month."

    def add_arguments(self, parser):
        parser.add_argument("--entity", required=True)
        parser.add_argument("--period", required=True, help="Fiscal period name, e.g. 2026-11")
        parser.add_argument("--as", dest="username", required=True)

    def handle(self, *args, entity, period, username, **opts):
        e = Entity.objects.filter(code=entity).first()
        p = FiscalPeriod.objects.filter(entity=e, name=period).first() if e else None
        user = User.objects.filter(username=username, is_active=True).first()
        if e is None or p is None or user is None:
            raise CommandError("Unknown entity, period or user.")
        try:
            run = services.run_billing(user, e, p)
        except BilingualError as exc:
            raise CommandError(exc.message_en)
        for row in run.log:
            self.stdout.write(f"{row['agreement']} {row['counterparty']}: {row['result']} {row.get('reason', '')}".rstrip())
        self.stdout.write(self.style.SUCCESS(f"Run {run.pk}: {run.invoices_created} draft invoice(s) created."))
