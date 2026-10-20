"""Review fixes of 2026-10-20 (second round; payroll §10.9 items 11 to 15, invoicing R128 as amended).

- bai' ajel rebates (finding 2): status ``rejected`` (ZATCA rejected the credit note; the instalments are restored;
  approved again or discarded), counted as open with ``draft``; ``credit_note_status`` and ``installment_cuts``. On
  PostgreSQL ``payroll_rebate_lock`` is replaced: never deleted; a draft changes freely; an approved rebate records
  its credit note final, or becomes ``rejected`` (only from ``issued``); a rejected one is approved again (new credit
  note, cuts) or discarded; nothing else changes.
- payroll payments (finding 3): ``payslip_ids`` (the payslips a salary payment pays) and ``payslip_parts`` (what it
  paid on each, at approval).
- WHT parts backfill (finding 4): a WHT payment approved before ``payroll.0017`` has no ``wht_parts`` and would drop
  out of the WHT return. Its per-payee parts are rebuilt from what was booked (the SAR base and WHT on 2220 stay as
  they were): a settlement payment's one employee; a run payment's remuneration payslips in its currency, split by
  their net pay (the old base was the net paid). Approved payments are locked on PostgreSQL
  (``payroll_payment_lock``), so the trigger is disabled for this one update inside the migration's transaction.
  A payment that cannot be rebuilt stays empty and the WHT return refuses it (``wht_parts_missing``).
"""
from decimal import ROUND_FLOOR, Decimal

from django.conf import settings
from django.db import migrations, models


CENT = Decimal("0.01")

REBATE_TRIGGER = r"""
CREATE OR REPLACE FUNCTION payroll_rebate_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    audit text[] := ARRAY['modified_by_id', 'modified_at'];
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
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND NEW.credit_note_status = 'final'
            AND changed <@ (ARRAY['credit_note_status'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'rejected' AND OLD.credit_note_status <> 'final'
            AND NEW.credit_note_status = 'rejected'
            AND changed <@ (ARRAY['status', 'credit_note_status'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'rejected' AND NEW.status = 'approved'
            AND changed <@ (ARRAY['status', 'credit_note_ref', 'credit_note_status', 'vat_amount', 'approved_by_id',
                                  'approved_at', 'installment_cuts'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'rejected' AND NEW.status = 'discarded' AND changed <@ (ARRAY['status'] || audit) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'bai'' ajel rebate % is % and locked', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;
"""

# payroll.0017's version, restored on the way back.
REBATE_TRIGGER_0017 = r"""
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
"""


def _split(total, weights):
    """``total`` (2 dp) over ``weights`` {key: Decimal}, largest remainder by cent, so the parts add up exactly."""
    whole = sum(weights.values(), Decimal("0"))
    if not whole:
        return None
    exact = {k: total * v / whole for k, v in weights.items()}
    parts = {k: v.quantize(CENT, rounding=ROUND_FLOOR) for k, v in exact.items()}
    rest = int((total - sum(parts.values(), Decimal("0"))) / CENT)
    for k in sorted(exact, key=lambda k: (parts[k] - exact[k], k))[:max(rest, 0)]:
        parts[k] += CENT
    return parts


def backfill_wht_parts(apps, schema_editor):
    Payment = apps.get_model("payroll", "PayrollPayment")
    Payslip = apps.get_model("payroll", "Payslip")
    Settlement = apps.get_model("payroll", "FinalSettlement")
    Employee = apps.get_model("hr", "Employee")
    updates = []
    for p in Payment.objects.filter(wht_category__isnull=False, status__in=("approved", "reversed")).order_by("pk"):
        if p.wht_parts or not (p.wht_amount_sar or p.wht_base_sar):
            continue
        weights = {}
        if p.kind == "final_settlement" and p.final_settlement_id:
            emp_id = Settlement.objects.filter(pk=p.final_settlement_id).values_list("employee_id", flat=True).first()
            if emp_id:
                weights = {emp_id: Decimal("1")}
        elif p.kind == "net_salaries" and p.run_id:
            slips = Payslip.objects.filter(run_id=p.run_id, refused=False, currency_id=p.currency_id)
            rem = slips.filter(employment_basis="remuneration")
            for emp_id, net in (rem if rem.exists() else slips).values_list("employee_id", "net_txn"):
                weights[emp_id] = weights.get(emp_id, Decimal("0")) + Decimal(net)
        base, wht = _split(Decimal(p.wht_base_sar), weights), _split(Decimal(p.wht_amount_sar), weights)
        if base is None or wht is None:
            continue  # nothing to split on: the WHT return refuses it (wht_parts_missing)
        emps = {e.pk: e for e in Employee.objects.filter(pk__in=weights)}  # country: encrypted field, decrypted here
        p.wht_parts = [{"employee_id": k, "employee_code": emps[k].employee_code,
                        "country": emps[k].country_of_residence or "", "base_sar": str(base[k]),
                        "wht_sar": str(wht[k]), "backfilled": True}
                       for k in sorted(weights, key=lambda k: emps[k].employee_code)]
        updates.append(p)
    if not updates:
        return
    pg = schema_editor.connection.vendor == "postgresql"
    if pg:  # deferred constraint checks first: a table with pending trigger events cannot be altered
        schema_editor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        schema_editor.execute("ALTER TABLE payroll_payrollpayment DISABLE TRIGGER payroll_payment_lock")
    try:
        for p in updates:
            models.QuerySet(Payment).filter(pk=p.pk).update(wht_parts=p.wht_parts)  # never a guarded manager
    finally:
        if pg:
            schema_editor.execute("ALTER TABLE payroll_payrollpayment ENABLE TRIGGER payroll_payment_lock")


def _sql(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards_guard(apps, schema_editor):
    _sql(schema_editor, REBATE_TRIGGER)


def backwards_guard(apps, schema_editor):
    _sql(schema_editor, REBATE_TRIGGER_0017)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_seed_access_v16_payroll_slice2"),
        ("hr", "0021_contract_pay_rate_basis"),
        ("payroll", "0017_review_fixes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="deferredsalerebate",
            name="payroll_one_open_rebate",
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="credit_note_status",
            field=models.CharField(
                blank=True,
                max_length=10,
                verbose_name="Credit note status / حالة الإشعار الدائن",
            ),
        ),
        migrations.AddField(
            model_name="deferredsalerebate",
            name="installment_cuts",
            field=models.JSONField(
                blank=True,
                default=list,
                verbose_name="Instalments reduced / الأقساط المخفضة",
            ),
        ),
        migrations.AddField(
            model_name="payrollpayment",
            name="payslip_ids",
            field=models.JSONField(
                blank=True,
                default=list,
                verbose_name="Payslips paid / القسائم المدفوعة",
            ),
        ),
        migrations.AddField(
            model_name="payrollpayment",
            name="payslip_parts",
            field=models.JSONField(
                blank=True,
                default=list,
                verbose_name="Paid per payslip / المدفوع لكل قسيمة",
            ),
        ),
        migrations.AlterField(
            model_name="deferredsalerebate",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft / مسودة"),
                    (
                        "approved",
                        "Approved (credit note issued) / معتمد (صدر الإشعار الدائن)",
                    ),
                    (
                        "rejected",
                        "Credit note rejected: approve again or discard / رُفض الإشعار الدائن: يُعتمد مجددًا أو يُلغى",
                    ),
                    ("discarded", "Discarded / ملغى"),
                ],
                default="draft",
                max_length=10,
                verbose_name="Status / الحالة",
            ),
        ),
        migrations.AlterField(
            model_name="payrollalert",
            name="kind",
            field=models.CharField(
                choices=[
                    (
                        "maternity_pending",
                        "Maternity case pending at GOSI / حالة أمومة بانتظار التأمينات",
                    ),
                    ("mudad_deadline", "Mudad upload due / موعد الرفع على مدد"),
                    (
                        "gosi_registration",
                        "GOSI registration due / موعد التسجيل في التأمينات",
                    ),
                    ("gosi_payment", "GOSI payment due / موعد سداد التأمينات"),
                    ("retention", "Retention review / مراجعة الحفظ"),
                    (
                        "leave_advance",
                        "Annual leave pay due in advance / أجر إجازة سنوية مستحق مقدمًا",
                    ),
                    (
                        "final_settlement_due",
                        "Final settlement due (Art. 88) / موعد المخالصة النهائية (المادة 88)",
                    ),
                    (
                        "maternity_decided",
                        "Maternity case decided: carried days to settle / قرار حالة الأمومة: أيام مرحّلة للتسوية",
                    ),
                    (
                        "bai_ajel_rebate",
                        "Bai' ajel rebate credit note rejected / رُفض الإشعار الدائن لإبراء بيع آجل",
                    ),
                ],
                max_length=20,
                verbose_name="Kind / النوع",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsalerebate",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("draft", "rejected"))),
                fields=("contract",),
                name="payroll_one_open_rebate",
            ),
        ),
        migrations.RunPython(forwards_guard, backwards_guard),
        migrations.RunPython(backfill_wht_parts, migrations.RunPython.noop),
    ]
