"""Seed the VAT return box definitions and the VAT late-payment rate, unverified (tax-returns.md §3.1, R6, OQ-2).

Every box number and label is [verify] against the current ZATCA VAT return form (tax/reference.py)."""
from django.db import migrations

from tax.reference import BOXES, BOXES_VALID_FROM, STATUTORY_RATES


def seed(apps, schema_editor):
    Box = apps.get_model("tax", "VatBoxDefinition")
    Rate = apps.get_model("tax", "StatutoryTaxRate")
    for n, (en, ar, side, has_amount, has_adj, has_vat, key, note) in BOXES.items():
        Box.objects.update_or_create(box_no=n, valid_from=BOXES_VALID_FROM, defaults=dict(
            label_en=en, label_ar=ar, side=side, has_amount=has_amount, has_adjustment=has_adj, has_vat=has_vat,
            mapping_key=key, verify_note=note))
    for code, (rate, valid_from, citation) in STATUTORY_RATES.items():
        Rate.objects.update_or_create(code=code, valid_from=valid_from, defaults=dict(rate=rate,
                                                                                      source_citation=citation))


def unseed(apps, schema_editor):
    apps.get_model("tax", "VatBoxDefinition").objects.filter(valid_from=BOXES_VALID_FROM).delete()
    apps.get_model("tax", "StatutoryTaxRate").objects.filter(code__in=list(STATUTORY_RATES)).delete()


class Migration(migrations.Migration):
    dependencies = [("tax", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
