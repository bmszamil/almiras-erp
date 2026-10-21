"""Review fixes of 2026-10-21 (fourth review of the ibra' path and payroll WHT; payroll §10.9 items 23 to 29).

- ``PayrollPayment.wht_uncovered`` (items 1, 6): per payee, the base and WHT not withheld because the WHT due was
  more than their own part of the payment; a ``review_required`` row of the WHT return.
- ``DeferredSaleClosure`` (item 5): a bai' ajel balance left outstanding after exit written off or recovered; the
  installment statuses ``written_off`` and ``recovered_after_exit``.
- PostgreSQL (``payroll_installment_lock``, 0014's body otherwise unchanged): a bai' ajel installment outstanding
  after exit moves only to ``written_off`` (status only) or to ``recovered_after_exit`` (status, amount reduced to
  what was recovered, ``deducted_amount`` = amount); both are final and never deleted. ``payroll_sale_closure_lock``:
  a closure is never deleted and, once approved, only receives its journal entry once
  (``payroll_posted_document_lock``).
"""
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models

INSTALLMENT_LOCK = r"""
CREATE OR REPLACE FUNCTION payroll_installment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('deducted', 'settled_in_final', 'outstanding_after_exit', 'written_off',
                          'recovered_after_exit') THEN
            RAISE EXCEPTION '% %: a deducted installment is never deleted (payroll R18, R13)', TG_TABLE_NAME, OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status NOT IN ('deducted', 'settled_in_final', 'outstanding_after_exit', 'written_off',
                          'recovered_after_exit') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'deducted' AND NEW.status = 'planned' AND NEW.deducted_amount = 0 AND NEW.run_id IS NULL
            AND changed <@ ARRAY['status', 'deducted_amount', 'run_id']
            AND EXISTS (SELECT 1 FROM payroll_payrollrun r WHERE r.id = OLD.run_id AND r.status = 'reversed') THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'outstanding_after_exit' AND NEW.status = 'written_off' AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'payroll_deferredsaleinstallment' AND OLD.status = 'outstanding_after_exit'
            AND NEW.status = 'recovered_after_exit' AND NEW.amount > 0 AND NEW.amount <= OLD.amount
            AND NEW.deducted_amount = NEW.amount AND changed <@ ARRAY['status', 'amount', 'deducted_amount'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% %: a deducted installment changes only when its run is reversed (payroll R13, R18)',
        TG_TABLE_NAME, OLD.id;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_sale_closure_lock ON payroll_deferredsaleclosure;
CREATE TRIGGER payroll_sale_closure_lock BEFORE UPDATE OR DELETE ON payroll_deferredsaleclosure
    FOR EACH ROW EXECUTE FUNCTION payroll_posted_document_lock();
"""

# 0014's installment function (loan installments only move outstanding_after_exit -> written_off).
PREVIOUS_INSTALLMENT_LOCK = r"""
DROP TRIGGER IF EXISTS payroll_sale_closure_lock ON payroll_deferredsaleclosure;

CREATE OR REPLACE FUNCTION payroll_installment_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('deducted', 'settled_in_final', 'outstanding_after_exit', 'written_off') THEN
            RAISE EXCEPTION '% %: a deducted installment is never deleted (payroll R18, R13)', TG_TABLE_NAME, OLD.id;
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status NOT IN ('deducted', 'settled_in_final', 'outstanding_after_exit', 'written_off') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'deducted' AND NEW.status = 'planned' AND NEW.deducted_amount = 0 AND NEW.run_id IS NULL
            AND changed <@ ARRAY['status', 'deducted_amount', 'run_id']
            AND EXISTS (SELECT 1 FROM payroll_payrollrun r WHERE r.id = OLD.run_id AND r.status = 'reversed') THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'payroll_loaninstallment' AND OLD.status = 'outstanding_after_exit'
            AND NEW.status = 'written_off' AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% %: a deducted installment changes only when its run is reversed (payroll R13, R18)',
        TG_TABLE_NAME, OLD.id;
END;
$$ LANGUAGE plpgsql;
"""


def _sql(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards_guard(apps, schema_editor):
    _sql(schema_editor, INSTALLMENT_LOCK)


def backwards_guard(apps, schema_editor):
    _sql(schema_editor, PREVIOUS_INSTALLMENT_LOCK)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_alter_accesslog_event"),
        ("hr", "0021_contract_pay_rate_basis"),
        ("ledger", "0021_bai_ajel_accounts"),
        ("payroll", "0019_review_fixes_rebate_token"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="payrollpayment",
            name="wht_uncovered",
            field=models.JSONField(
                blank=True,
                default=list,
                verbose_name="WHT not withheld per payee / ضريبة الاستقطاع غير المستقطعة لكل مستفيد",
            ),
        ),
        migrations.AlterField(
            model_name="deferredsaleinstallment",
            name="status",
            field=models.CharField(
                choices=[
                    ("planned", "Planned / مخطط"),
                    ("deducted", "Deducted / مستقطع"),
                    (
                        "settled_in_final",
                        "Settled in the final settlement / سُدد في المخالصة النهائية",
                    ),
                    (
                        "outstanding_after_exit",
                        "Outstanding after exit / قائم بعد انتهاء الخدمة",
                    ),
                    ("written_off", "Written off / مشطوب"),
                    (
                        "recovered_after_exit",
                        "Recovered from the former employee / مُسترد من الموظف السابق",
                    ),
                ],
                default="planned",
                max_length=24,
                verbose_name="Status / الحالة",
            ),
        ),
        migrations.CreateModel(
            name="DeferredSaleClosure",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("write_off", "Write-off / شطب"),
                            (
                                "recovery",
                                "Recovered from the former employee / استرداد من الموظف السابق",
                            ),
                        ],
                        max_length=10,
                        verbose_name="Kind / النوع",
                    ),
                ),
                (
                    "number",
                    models.CharField(
                        blank=True, max_length=40, verbose_name="Number / الرقم"
                    ),
                ),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Balance closed (1240) / الرصيد المقفل (1240)",
                    ),
                ),
                (
                    "profit_share",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("0"),
                        max_digits=18,
                        verbose_name="Unearned profit reversed (1241) / الربح غير المحقق المعكوس (1241)",
                    ),
                ),
                (
                    "received_on",
                    models.DateField(
                        blank=True,
                        null=True,
                        verbose_name="Received on / تاريخ الاستلام",
                    ),
                ),
                (
                    "bank_reference",
                    models.CharField(
                        blank=True,
                        max_length=60,
                        verbose_name="Bank reference / المرجع البنكي",
                    ),
                ),
                (
                    "reason_en",
                    models.TextField(
                        blank=True,
                        verbose_name="Reason (English) / السبب (بالإنجليزية)",
                    ),
                ),
                (
                    "reason_ar",
                    models.TextField(verbose_name="Reason (Arabic) / السبب (بالعربية)"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft / مسودة"),
                            ("submitted", "Submitted / مقدّم"),
                            ("approved", "Approved and posted / معتمد ومرحّل"),
                            ("rejected", "Rejected / مرفوض"),
                        ],
                        default="draft",
                        max_length=10,
                        verbose_name="Status / الحالة",
                    ),
                ),
                (
                    "installment_parts",
                    models.JSONField(
                        blank=True,
                        default=list,
                        verbose_name="Instalments closed / الأقساط المقفلة",
                    ),
                ),
                (
                    "submitted_at",
                    models.DateTimeField(
                        blank=True,
                        null=True,
                        verbose_name="Submitted at / تاريخ التقديم",
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
                (
                    "rejected_reason_ar",
                    models.TextField(
                        blank=True,
                        verbose_name="Rejection reason (Arabic) / سبب الرفض (بالعربية)",
                    ),
                ),
                (
                    "approved_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Approved by / اعتمده",
                    ),
                ),
                (
                    "bank_account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.account",
                        verbose_name="Bank account / الحساب البنكي",
                    ),
                ),
                (
                    "contract",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="closures",
                        to="payroll.deferredsalecontract",
                        verbose_name="Contract / العقد",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Created by / أنشأه",
                    ),
                ),
                (
                    "employee",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="hr.employee",
                        verbose_name="Employee / الموظف",
                    ),
                ),
                (
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.entity",
                        verbose_name="Entity / الكيان",
                    ),
                ),
                (
                    "expense_account",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.account",
                        verbose_name="Write-off account / حساب الشطب",
                    ),
                ),
                (
                    "journal_entry",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.journalentry",
                        verbose_name="Journal entry / القيد",
                    ),
                ),
                (
                    "modified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Last modified by / آخر من عدّله",
                    ),
                ),
                (
                    "submitted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Submitted by / قدّمه",
                    ),
                ),
            ],
            options={
                "verbose_name": "bai' ajel balance after exit (write-off or recovery) / رصيد بيع آجل بعد انتهاء الخدمة (شطب أو استرداد)",
                "verbose_name_plural": "bai' ajel balances after exit / أرصدة البيع الآجل بعد انتهاء الخدمة",
                "base_manager_name": "objects",
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="payroll_sale_closure_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("profit_share__gte", 0),
                            ("profit_share__lte", models.F("amount")),
                        ),
                        name="payroll_sale_closure_profit_share",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("status__in", ("draft", "submitted"))),
                        fields=("contract",),
                        name="payroll_one_open_sale_closure",
                    ),
                ],
            },
        ),
        migrations.RunPython(forwards_guard, backwards_guard),
    ]
