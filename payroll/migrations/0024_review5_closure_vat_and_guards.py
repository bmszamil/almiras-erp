"""Review 5 of 2026-10-21 (bai' ajel closures after exit; payroll §10.9 items 30 to 38).

- ``DeferredSaleClosure.vat_share`` (item 5): the VAT included in a balance written off (it stays on 2200), listed
  for the tax adviser (bad-debt relief, invoicing OQ-78); ``warnings``: the en/ar notes shown to the approver.
- PostgreSQL (item 8): ``payroll_installment_lock`` (0020's body otherwise unchanged): a bai' ajel installment
  outstanding after exit becomes ``written_off`` only when an approved write-off closure of its contract lists it in
  its ``installment_parts``, and ``recovered_after_exit`` only when an approved recovery closure of its contract lists
  it at that amount (the approval stores the closure as approved with its parts before it closes the
  installments). ``payroll_sale_closure_lock``: a closure is never deleted; approved, it only receives its journal
  entry once; rejected, it never changes.
"""
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models

_COMMON_HEAD = r"""
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
"""

_COMMON_TAIL = r"""
    RAISE EXCEPTION '% %: a deducted installment changes only when its run is reversed (payroll R13, R18)',
        TG_TABLE_NAME, OLD.id;
END;
$$ LANGUAGE plpgsql;
"""

INSTALLMENT_LOCK = _COMMON_HEAD + r"""
    IF OLD.status = 'outstanding_after_exit' AND NEW.status = 'written_off' AND changed <@ ARRAY['status'] THEN
        IF TG_TABLE_NAME = 'payroll_loaninstallment' THEN
            RETURN NEW;
        END IF;
        IF EXISTS (SELECT 1 FROM payroll_deferredsaleclosure c
                    WHERE c.contract_id = OLD.contract_id AND c.entity_id = OLD.entity_id AND c.status = 'approved'
                      AND c.kind = 'write_off'
                      AND c.installment_parts @> jsonb_build_array(jsonb_build_object('installment_id', OLD.id))) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '% %: a bai'' ajel installment is written off only by an approved write-off that lists it',
            TG_TABLE_NAME, OLD.id;
    END IF;
    IF TG_TABLE_NAME = 'payroll_deferredsaleinstallment' AND OLD.status = 'outstanding_after_exit'
            AND NEW.status = 'recovered_after_exit' AND NEW.amount > 0 AND NEW.amount <= OLD.amount
            AND NEW.deducted_amount = NEW.amount AND changed <@ ARRAY['status', 'amount', 'deducted_amount'] THEN
        IF EXISTS (SELECT 1 FROM payroll_deferredsaleclosure c, jsonb_array_elements(c.installment_parts) e
                    WHERE c.contract_id = OLD.contract_id AND c.entity_id = OLD.entity_id AND c.status = 'approved'
                      AND c.kind = 'recovery' AND (e ->> 'installment_id')::bigint = OLD.id
                      AND (e ->> 'amount')::numeric = NEW.amount) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION '% %: a bai'' ajel installment is recovered only by an approved recovery listing it',
            TG_TABLE_NAME, OLD.id;
    END IF;
""" + _COMMON_TAIL

# 0020's function.
PREVIOUS_INSTALLMENT_LOCK = _COMMON_HEAD + r"""
    IF OLD.status = 'outstanding_after_exit' AND NEW.status = 'written_off' AND changed <@ ARRAY['status'] THEN
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'payroll_deferredsaleinstallment' AND OLD.status = 'outstanding_after_exit'
            AND NEW.status = 'recovered_after_exit' AND NEW.amount > 0 AND NEW.amount <= OLD.amount
            AND NEW.deducted_amount = NEW.amount AND changed <@ ARRAY['status', 'amount', 'deducted_amount'] THEN
        RETURN NEW;
    END IF;
""" + _COMMON_TAIL

CLOSURE_LOCK = r"""
CREATE OR REPLACE FUNCTION payroll_sale_closure_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION '% % is never deleted (payroll R49)', TG_TABLE_NAME, OLD.id;
    END IF;
    IF OLD.status NOT IN ('approved', 'rejected') THEN
        RETURN NEW;
    END IF;
    changed := payroll_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND changed <@ ARRAY['journal_entry_id'] AND OLD.journal_entry_id IS NULL THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION '% % is % and locked (payroll R49)', TG_TABLE_NAME, OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS payroll_sale_closure_lock ON payroll_deferredsaleclosure;
CREATE TRIGGER payroll_sale_closure_lock BEFORE UPDATE OR DELETE ON payroll_deferredsaleclosure
    FOR EACH ROW EXECUTE FUNCTION payroll_sale_closure_lock();
"""

PREVIOUS_CLOSURE_LOCK = r"""
DROP TRIGGER IF EXISTS payroll_sale_closure_lock ON payroll_deferredsaleclosure;
DROP FUNCTION IF EXISTS payroll_sale_closure_lock();
CREATE TRIGGER payroll_sale_closure_lock BEFORE UPDATE OR DELETE ON payroll_deferredsaleclosure
    FOR EACH ROW EXECUTE FUNCTION payroll_posted_document_lock();
"""


def _sql(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards_guard(apps, schema_editor):
    _sql(schema_editor, INSTALLMENT_LOCK + CLOSURE_LOCK)


def backwards_guard(apps, schema_editor):
    _sql(schema_editor, PREVIOUS_INSTALLMENT_LOCK + PREVIOUS_CLOSURE_LOCK)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0027_seed_access_v17_payroll_slice3"),
        ("hr", "0021_contract_pay_rate_basis"),
        ("ledger", "0021_bai_ajel_accounts"),
        ("payroll", "0023_slice3_review_fixes"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="deferredsaleclosure",
            name="vat_share",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0"),
                max_digits=18,
                verbose_name="VAT in the balance written off (stays on 2200) / ضريبة القيمة المضافة في الرصيد المشطوب (تبقى في 2200)",
            ),
        ),
        migrations.AddField(
            model_name="deferredsaleclosure",
            name="warnings",
            field=models.JSONField(
                blank=True, default=list, verbose_name="Warnings / تنبيهات"
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsaleclosure",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("vat_share__gte", 0), ("vat_share__lte", models.F("amount"))
                ),
                name="payroll_sale_closure_vat_share",
            ),
        ),
        migrations.RunPython(forwards_guard, backwards_guard),
    ]
