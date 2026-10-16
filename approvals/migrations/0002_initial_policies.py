"""Register manual journal entries as approvable and load the starting matrix.

R6: one group-wide policy per spending type (is_spending=True), any amount, one step: managing_partner.
Spending types (payments, bills, ...) are registered by their own modules' migrations, which call
approvals.services.ensure_spending_policies() the same way.
Manual journal entries are not a spending type; by D2 ("MP approves all") and until OQ3 is answered,
they get the same single MP step (see Build notes).
"""
import datetime

from django.db import migrations


def forwards(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    ApprovableType = apps.get_model("approvals", "ApprovableType")
    ApprovalPolicy = apps.get_model("approvals", "ApprovalPolicy")
    ApprovalStep = apps.get_model("approvals", "ApprovalStep")
    Role = apps.get_model("access", "Role")

    ct, _ = ContentType.objects.get_or_create(app_label="ledger", model="journalentry")
    je, _ = ApprovableType.objects.get_or_create(
        code="ledger.journal_entry",
        defaults={"name_en": "Manual journal entry", "name_ar": "قيد يومية يدوي", "content_type": ct,
                  "is_spending": False},
    )
    mp = Role.objects.get(code="managing_partner")
    types = list(ApprovableType.objects.filter(is_spending=True)) + [je]
    for t in types:
        if ApprovalPolicy.objects.filter(document_type=t, entity__isnull=True, currency="SAR").exists():
            continue
        p = ApprovalPolicy.objects.create(
            document_type=t, currency="SAR", priority=0, valid_from=datetime.date(2026, 1, 1),
            name_en=f"{t.name_en}: Managing Partner approves all",
            name_ar=f"{t.name_ar}: يعتمد الشريك المدير الجميع",
        )
        ApprovalStep.objects.create(policy=p, sequence=1, approver_role=mp, min_approvals=1)


class Migration(migrations.Migration):
    dependencies = [
        ("approvals", "0001_initial"),
        ("access", "0002_seed_roles"),
        ("ledger", "0001_initial"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
