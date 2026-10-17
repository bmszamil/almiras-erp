"""Set up a new entity: chart of accounts from the group template, and monthly periods."""
import calendar
from datetime import date

from django.db import transaction

from .coa_template import TEMPLATE
from .models import Account, FiscalPeriod


@transaction.atomic
def install_chart_of_accounts(entity):
    headers, by_code = {}, {}
    created = 0
    for code, en, ar, typ, flags in TEMPLATE:
        parent = None if flags.get("header") else headers.get(code[0])
        if flags.get("parent"):
            parent = by_code.get(flags["parent"]) or Account.objects.get(entity=entity, code=flags["parent"])
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
        by_code[code] = acc
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


@transaction.atomic
def install_bank_sub_account(entity, code, name_en, name_ar, currency):
    """One GL sub-account under 1100 for one bank account (banking R5, R6; OQ-5 default), as setup data.

    Asset, postable, monetary (revalued when foreign, ledger-fx R21), restricted to the bank account's currency.
    Idempotent for the same code and currency; an existing account with that code that is not such a sub-account
    is refused rather than changed.
    """
    parent = Account.objects.get(entity=entity, code="1100")
    currency_code = getattr(currency, "code", currency)
    account, created = Account.objects.get_or_create(
        entity=entity, code=code,
        defaults=dict(name_en=name_en, name_ar=name_ar, type=Account.Type.ASSET, parent=parent, is_postable=True,
                      is_monetary=True, currency_id=currency_code),
    )
    if not created and (account.parent_id != parent.pk or account.currency_id != currency_code):
        raise ValueError(f"Account {code} of {entity.code} exists and is not a {currency_code} sub-account of 1100. / "
                         f"الحساب {code} للكيان {entity.code} موجود وليس حسابًا فرعيًا للحساب 1100 بعملة {currency_code}.")
    return account
