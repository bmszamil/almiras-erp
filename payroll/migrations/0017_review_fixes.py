"""Payroll review and compliance fixes (2026-10-20): asset type on bai' ajel contracts (A3), the ibra' rebate (A8,
invoicing R128), the WHT parts per payee (B2, B3), the due date of an increasing settlement correction (C1) and one open correction per settlement
(review 6). On PostgreSQL (nothing on SQLite):

- ``invoicing_purchasedocument`` (review 1, invoicing R124; trigger ``invoicing_bai_ajel_contract_lock``, named to
  fire before invoicing's own document lock): a bill recorded as a bai' ajel contract's acquisition is
  never reversed, and a vendor credit note with a line on 1250 is never posted against it (the active ``DeferredSale``
  side is ``invoicing.0031``).
- settlement corrections: the approval may also set ``due_date`` (C1).
- bai' ajel rebates: never deleted; an approved rebate changes only its credit note reference when a rejected
  credit note is replaced.
"""

import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models

TRIGGER = r"""
CREATE OR REPLACE FUNCTION payroll_bai_ajel_bill_lock() RETURNS trigger AS $$
BEGIN
    IF NEW.doc_type = 'bill' AND OLD.status <> 'reversed' AND NEW.status = 'reversed'
            AND EXISTS (SELECT 1 FROM payroll_deferredsalecontract c WHERE c.purchase_bill_id = OLD.id
                        AND c.status <> 'discarded') THEN
        RAISE EXCEPTION 'bill % is a bai'' ajel contract''s acquisition and is never reversed (invoicing R124)', OLD.id;
    END IF;
    IF NEW.doc_type = 'vendor_credit_note' AND OLD.status <> 'posted' AND NEW.status = 'posted'
            AND NEW.original_document_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM payroll_deferredsalecontract c WHERE c.purchase_bill_id = NEW.original_document_id
                        AND c.status <> 'discarded')
            AND EXISTS (SELECT 1 FROM invoicing_purchasedocumentline l JOIN ledger_account a ON a.id = l.account_id
                        WHERE l.document_id = NEW.id AND a.code = '1250') THEN
        RAISE EXCEPTION 'vendor credit note %: its bill is a bai'' ajel acquisition; 1250 is never credited (R124)',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS invoicing_bai_ajel_contract_lock ON invoicing_purchasedocument;
CREATE TRIGGER invoicing_bai_ajel_contract_lock BEFORE UPDATE ON invoicing_purchasedocument
    FOR EACH ROW EXECUTE FUNCTION payroll_bai_ajel_bill_lock();

CREATE OR REPLACE FUNCTION payroll_correction_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    audit text[] := ARRAY['modified_by_id', 'modified_at'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'settlement correction % is never deleted', OLD.id;
    END IF;
    IF OLD.status NOT IN ('submitted', 'approved') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'returned'
            AND changed <@ (ARRAY['status', 'returned_reason_ar'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'submitted' AND NEW.status = 'approved' AND OLD.number = '' AND NEW.number <> ''
            AND changed <@ ARRAY['status', 'number', 'approved_by_id', 'approved_at', 'warnings', 'due_date'] THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND changed <@ ARRAY['journal_entry_id']
            AND OLD.journal_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'settlement correction % is % and locked', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION payroll_rebate_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bai'' ajel rebate % is never deleted', OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'approved'
            AND changed <@ ARRAY['credit_note_ref', 'modified_by_id', 'modified_at'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'bai'' ajel rebate % is % and locked', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_rebate_lock ON payroll_deferredsalerebate;
CREATE TRIGGER payroll_rebate_lock BEFORE UPDATE OR DELETE ON payroll_deferredsalerebate
    FOR EACH ROW EXECUTE FUNCTION payroll_rebate_lock();
"""

DROP = r"""
DROP TRIGGER IF EXISTS invoicing_bai_ajel_contract_lock ON invoicing_purchasedocument;
DROP FUNCTION IF EXISTS payroll_bai_ajel_bill_lock();
DROP TRIGGER IF EXISTS payroll_rebate_lock ON payroll_deferredsalerebate;
DROP FUNCTION IF EXISTS payroll_rebate_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, TRIGGER)


def backwards(apps, schema_editor):
    _run(schema_editor, DROP)  # the 0014 correction lock is restored by migrating 0014 again


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_seed_access_v16_payroll_slice2"),
        ("hr", "0021_contract_pay_rate_basis"),
        ("ledger", "0021_bai_ajel_accounts"),
        ("payroll", "0016_bai_ajel_sale_invoiced"),
        ("invoicing", "0031_deferred_sale_review_guards"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DeferredSaleRebate",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Created at / تاريخ الإنشاء"
                    ),
                ),
                (
                    "modified_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="Last modified at / تاريخ آخر تعديل",
                    ),
                ),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Profit waived (net of VAT) / الربح المُبرأ منه (قبل الضريبة)",
                    ),
                ),
                (
                    "vat_amount",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0"),
                        max_digits=18,
                        verbose_name="VAT credited / الضريبة المخفضة",
                    ),
                ),
                (
                    "reason_ar",
                    models.TextField(verbose_name="Reason (Arabic) / السبب (بالعربية)"),
                ),
                (
                    "reason_en",
                    models.TextField(
                        blank=True,
                        verbose_name="Reason (English) / السبب (بالإنجليزية)",
                    ),
                ),
                (
                    "request_ref",
                    models.CharField(
                        max_length=200,
                        verbose_name="Employee's written request / طلب الموظف المكتوب",
                    ),
                ),
                (
                    "credit_note_ref",
                    models.CharField(
                        blank=True,
                        max_length=60,
                        verbose_name="Credit note (invoicing) / الإشعار الدائن",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft / مسودة"),
                            (
                                "approved",
                                "Approved (credit note issued) / معتمد (صدر الإشعار الدائن)",
                            ),
                            ("discarded", "Discarded / ملغى"),
                        ],
                        default="draft",
                        max_length=10,
                        verbose_name="Status / الحالة",
                    ),
                ),
                (
                    "approved_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="Approved at / تاريخ الاعتماد",
                    ),
                ),
            ],
            options={
                "verbose_name": "bai' ajel rebate (ibra') / إبراء بيع آجل",
                "verbose_name_plural": "bai' ajel rebates (ibra') / إبراءات البيع الآجل",
                "base_manager_name": "objects",
            },
        ),
        migrations.AddField(
            model_name="deferredsalecontract",
            name="asset_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("vehicle", "Vehicle / مركبة"),
                    ("equipment", "Equipment or appliance / معدات أو أجهزة"),
                    ("electronics", "Electronics / إلكترونيات"),
                    ("furniture", "Furniture / أثاث"),
                    ("other_movable", "Other movable goods / منقولات أخرى"),
                    (
                        "real_estate",
                        "Real estate (refused: RETT) / عقار (مرفوض: ضريبة التصرفات العقارية)",
                    ),
                ],
                max_length=20,
                verbose_name="Asset type / نوع الأصل",
            ),
        ),
        migrations.AddField(
            model_name="settlementcorrection",
            name="due_date",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="Increase due by / تاريخ استحقاق الزيادة",
            ),
        ),
        migrations.AddConstraint(
            model_name="settlementcorrection",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("draft", "submitted", "returned"))),
                fields=("settlement",),
                name="payroll_one_open_correction",
            ),
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Approved by / اعتمده",
            ),
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="contract",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="rebates",
                to="payroll.deferredsalecontract",
                verbose_name="Contract / العقد",
            ),
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by / أنشأه",
            ),
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="entity",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.entity",
                verbose_name="Entity / الكيان",
            ),
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="modified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Last modified by / آخر من عدّله",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsalerebate",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "draft")),
                fields=("contract",),
                name="payroll_one_open_rebate",
            ),
        ),
        migrations.AddField(
            model_name="payrollpayment",
            name="wht_parts",
            field=models.JSONField(blank=True, default=list, verbose_name="WHT per payee / ضريبة الاستقطاع لكل مستفيد"),
        ),
        migrations.RunPython(forwards, backwards),
    ]
