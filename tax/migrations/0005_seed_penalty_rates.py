"""Seed the late-filing and incorrect-return rates (C17, PSG §14) and update the late-payment citation (C16).

Display only (R13 alerts); rows stay unverified until a person verifies them (R6)."""
from django.db import migrations

from tax.reference import STATUTORY_RATES, STATUTORY_RATES_0005


def seed(apps, schema_editor):
    Rate = apps.get_model("tax", "StatutoryTaxRate")
    for code, (rate, valid_from, citation) in {**STATUTORY_RATES, **STATUTORY_RATES_0005}.items():
        Rate.objects.update_or_create(code=code, valid_from=valid_from, defaults=dict(rate=rate,
                                                                                      source_citation=citation))


def unseed(apps, schema_editor):
    apps.get_model("tax", "StatutoryTaxRate").objects.filter(code__in=list(STATUTORY_RATES_0005)).delete()


class Migration(migrations.Migration):
    dependencies = [("tax", "0004_review_fixes")]

    operations = [migrations.RunPython(seed, unseed)]
