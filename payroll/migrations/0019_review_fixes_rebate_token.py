"""Review fixes of 2026-10-20 (third round; payroll §10.9 items 17 to 22, invoicing R128 as amended).

- bai' ajel rebates (finding 4): ``approval_token``, a one-time token the rebate approval writes on the locked row
  and clears when done; invoicing's call through ``invoicing_port.rebate_problems`` must carry it.
- ``payroll_rebate_lock`` (finding 3, PostgreSQL; payroll.0018 is not edited): a rejected rebate is approved again
  only with a new credit note (``credit_note_ref`` changed, not empty), whose status is ``issued`` or ``final``, an
  approver and the token cleared; while rejected only the token changes (the approval in progress); the credit note
  becomes ``final`` once, from a note not final yet; ``approved`` -> ``rejected`` only from a note not final; a final
  note's rebate never changes; the amount never changes once approved; never deleted.
"""
from importlib import import_module

from django.db import migrations, models


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
    IF OLD.status = 'approved' AND NEW.status = 'approved' AND OLD.credit_note_status <> 'final'
            AND NEW.credit_note_status = 'final' AND changed <@ (ARRAY['credit_note_status'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'rejected' AND OLD.credit_note_status <> 'final'
            AND NEW.credit_note_status = 'rejected'
            AND changed <@ (ARRAY['status', 'credit_note_status'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'rejected' AND NEW.status = 'rejected' AND changed <@ ARRAY['approval_token'] THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'rejected' AND NEW.status = 'approved' AND NEW.credit_note_status IN ('issued', 'final')
            AND NEW.credit_note_ref <> '' AND NEW.credit_note_ref <> OLD.credit_note_ref
            AND NEW.approved_by_id IS NOT NULL AND NEW.approval_token = ''
            AND changed <@ (ARRAY['status', 'credit_note_ref', 'credit_note_status', 'vat_amount', 'approved_by_id',
                                  'approved_at', 'installment_cuts', 'approval_token'] || audit) THEN
        RETURN NEW;
    END IF;
    IF OLD.status = 'rejected' AND NEW.status = 'discarded' AND changed <@ (ARRAY['status'] || audit) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'bai'' ajel rebate % is % and locked', OLD.id, OLD.status;
END;
$$ LANGUAGE plpgsql;
"""


def _sql(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards_guard(apps, schema_editor):
    _sql(schema_editor, REBATE_TRIGGER)


def backwards_guard(apps, schema_editor):
    _sql(schema_editor, import_module("payroll.migrations.0018_review_fixes_rebate_wht").REBATE_TRIGGER)


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0018_review_fixes_rebate_wht"),
    ]

    operations = [
        migrations.AddField(
            model_name="deferredsalerebate",
            name="approval_token",
            field=models.CharField(
                blank=True,
                editable=False,
                max_length=64,
                verbose_name="Approval token (one-time) / رمز الاعتماد (لمرة واحدة)",
            ),
        ),
        migrations.RunPython(forwards_guard, backwards_guard),
    ]
