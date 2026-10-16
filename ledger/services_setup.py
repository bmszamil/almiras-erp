"""Set up a new entity: chart of accounts from the group template, and monthly periods."""
import calendar
from datetime import date

from django.db import transaction

from .coa_template import TEMPLATE
from .models import Account, FiscalPeriod


@transaction.atomic
def install_chart_of_accounts(entity):
    headers = {}
    created = 0
    for code, en, ar, typ, flags in TEMPLATE:
        parent = None if flags.get("header") else headers.get(code[0])
        acc, was_created = Account.objects.get_or_create(
            entity=entity,
            code=code,
            defaults=dict(
                name_en=en,
                name_ar=ar,
                type=typ,
                parent=parent,
                is_postable=not (flags.get("header", False) or flags.get("report_only", False)),
                is_intercompany=flags.get("intercompany", False),
                sharia_prohibited=flags.get("prohibited", False),
                requires_purification=flags.get("purify", False),
                is_monetary=flags.get("monetary", False),
            ),
        )
        if flags.get("header"):
            headers[code[0]] = acc
        created += was_created
    return created


@transaction.atomic
def create_monthly_periods(entity, year):
    created = 0
    for month in range(1, 13):
        last = calendar.monthrange(year, month)[1]
        _, was_created = FiscalPeriod.objects.get_or_create(
            entity=entity,
            name=f"{year}-{month:02d}",
            defaults=dict(start_date=date(year, month, 1), end_date=date(year, month, last)),
        )
        created += was_created
    return created
