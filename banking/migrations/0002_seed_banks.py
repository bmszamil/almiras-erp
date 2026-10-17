"""Seed the five banks of decision B1 (banking 3.1). Codes as payroll ``salary_bank``.

The IBAN bank codes and BICs are written from memory and seeded ``verified = False`` **[CC-verify]**: the
accountant marks a bank verified after the compliance check (OQ-4). Until then a mismatch of the IBAN bank code is a
warning, not a refusal. Arabic names are for review by the accountant [AR-review].
"""
from django.db import migrations

BANKS_V1 = {
    # code: (name_en, name_ar, country, sa_bank_code, swift_bic)
    "riyad": ("Riyad Bank", "بنك الرياض", "SA", "20", "RIBLSARI"),
    "snb": ("Saudi National Bank", "البنك الأهلي السعودي", "SA", "10", "NCBKSAJE"),
    "alrajhi": ("Al Rajhi Bank", "مصرف الراجحي", "SA", "80", "RJHISARI"),
    "aljazira": ("Bank AlJazira", "بنك الجزيرة", "SA", "60", "BJAZSAJE"),
    "alinma": ("Alinma Bank", "مصرف الإنماء", "SA", "05", "INMASARI"),
}


def seed(apps, schema_editor):
    Bank = apps.get_model("banking", "Bank")
    for code, (en, ar, country, sa_code, bic) in BANKS_V1.items():
        Bank.objects.update_or_create(code=code, defaults=dict(
            name_en=en, name_ar=ar, country=country, sa_bank_code=sa_code, swift_bic=bic, verified=False,
            is_active=True))


def unseed(apps, schema_editor):
    apps.get_model("banking", "Bank").objects.filter(code__in=list(BANKS_V1)).delete()


class Migration(migrations.Migration):
    dependencies = [("banking", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
