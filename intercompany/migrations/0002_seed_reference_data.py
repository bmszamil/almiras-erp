"""Reference data for intercompany billing.

- Service types from spec 3.2 with the default accounts of spec 5.6 (proposed; accountant to confirm, Q8).
- Standard KSA VAT rate 15% from 1 Jul 2020 (tax settings, R8).
- Approvable types for agreements, invoices/credit notes and mirror bills, each with one group-wide
  policy: one step, Managing Partner (spec D7, approvals D2/R6).
"""
import datetime
from decimal import Decimal

from django.db import migrations

SERVICE_TYPES = [
    # code, English, Arabic, category, income, expense
    ("FIN", "Finance and accounting services", "خدمات المالية والمحاسبة", "service", "4210", "5300"),
    ("HR", "HR and payroll services", "خدمات الموارد البشرية والرواتب", "service", "4210", "5300"),
    ("LEGAL", "Legal and governance services", "الخدمات القانونية والحوكمة", "service", "4210", "5300"),
    ("RENT_ACCOM", "Staff accommodation rental", "تأجير سكن الموظفين", "rental", "4110", "5200"),
    ("RENT_WH", "Warehouse space rental", "تأجير مساحات المستودعات", "rental", "4110", "5200"),
    ("RENT_MACH", "Machine rental", "تأجير الآلات", "rental", "4110", "5200"),
    ("RENT_VEH", "Vehicle rental", "تأجير السيارات", "rental", "4110", "5200"),
    ("RENT_OFFICE", "Office space rental", "تأجير المكاتب", "rental", "4110", "5200"),
    ("IP", "IP licence", "ترخيص الملكية الفكرية", "ip", "4300", "5900"),
    ("SECOND", "Secondment of experts", "إعارة الخبراء", "secondment", "4210", "5100"),
    ("CONSULT", "Consultation", "الاستشارات", "consultation", "4210", "5300"),
]

APPROVABLE = [
    # code, model, English, Arabic, is_spending
    ("intercompany.agreement", "serviceagreement", "Intercompany service agreement", "اتفاقية خدمات بين شركات المجموعة", False),
    ("intercompany.invoice", "intercompanyinvoice", "Intercompany invoice / credit note", "فاتورة / إشعار دائن بين شركات المجموعة", False),
    ("intercompany.bill", "intercompanybill", "Intercompany bill (mirror)", "فاتورة مقابلة من شركة في المجموعة", True),
]


def forwards(apps, schema_editor):
    ServiceType = apps.get_model("intercompany", "ServiceType")
    VatRate = apps.get_model("intercompany", "VatRate")
    ContentType = apps.get_model("contenttypes", "ContentType")
    ApprovableType = apps.get_model("approvals", "ApprovableType")
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    Role = apps.get_model("access", "Role")

    for code, en, ar, category, income, expense in SERVICE_TYPES:
        ServiceType.objects.get_or_create(code=code, defaults={
            "name_en": en, "name_ar": ar, "category": category,
            "income_account_code": income, "expense_account_code": expense,
        })
    VatRate.objects.get_or_create(country="SA", valid_from=datetime.date(2020, 7, 1), defaults={"rate": Decimal("15.00")})

    mp = Role.objects.get(code="managing_partner")
    for code, model, en, ar, spending in APPROVABLE:
        ct, _ = ContentType.objects.get_or_create(app_label="intercompany", model=model)
        t, _ = ApprovableType.objects.get_or_create(
            code=code, defaults={"name_en": en, "name_ar": ar, "content_type": ct, "is_spending": spending})
        if ApprovalPolicy.objects.filter(document_type=t, entity__isnull=True, currency="SAR").exists():
            continue
        p = ApprovalPolicy.objects.create(
            document_type=t, currency="SAR", priority=0, valid_from=datetime.date(2026, 1, 1),
            name_en=f"{en}: Managing Partner approves all", name_ar=f"{ar}: يعتمد الشريك المدير الجميع",
        )
        ApprovalStep.objects.create(policy=p, sequence=1, approver_role=mp, min_approvals=1)


class Migration(migrations.Migration):
    dependencies = [
        ("intercompany", "0001_initial"),
        ("approvals", "0003_review_a_editors"),
        ("access", "0006_seed_intercompany_permissions"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
