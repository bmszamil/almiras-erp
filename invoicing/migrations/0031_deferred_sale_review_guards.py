"""Bai' ajel review fixes (2026-10-20), PostgreSQL only (nothing on SQLite):

- ``invoicing_deferredsale``: ``active`` goes from true to false **only when its sales document is rejected** by
  ZATCA (R112, R123 as amended); every other change is refused as before.
- ``invoicing_purchasedocument`` (R124): a bill backing an active bai' ajel sale is never reversed, and a vendor
  credit note with a line on 1250 is never posted against it. The payroll contract side (a bill recorded as a
  contract's acquisition) is guarded by ``payroll.0017``.
"""
from django.db import migrations

TRIGGER = r"""
CREATE OR REPLACE FUNCTION invoicing_deferredsale_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bai'' ajel sale % is never deleted (R112)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.active THEN
            RAISE EXCEPTION 'a bai'' ajel sale is created active through invoicing.services (R112)';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT changed <@ ARRAY['active'] OR NOT OLD.active OR NEW.active THEN
        RAISE EXCEPTION 'bai'' ajel sale % never changes; only a rejected sale invoice deactivates it (R112)', OLD.id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM invoicing_salesdocument d WHERE d.id = OLD.sales_document_id
                   AND d.status = 'rejected') THEN
        RAISE EXCEPTION 'bai'' ajel sale % is deactivated only when ZATCA rejected its sale invoice (R123)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION invoicing_bai_ajel_bill_lock() RETURNS trigger AS $$
BEGIN
    IF NEW.doc_type = 'bill' AND OLD.status <> 'reversed' AND NEW.status = 'reversed'
            AND EXISTS (SELECT 1 FROM invoicing_deferredsale s WHERE s.purchase_bill_id = OLD.id AND s.active) THEN
        RAISE EXCEPTION 'bill % backs a bai'' ajel sale and is never reversed (R124)', OLD.id;
    END IF;
    IF NEW.doc_type = 'vendor_credit_note' AND OLD.status <> 'posted' AND NEW.status = 'posted'
            AND NEW.original_document_id IS NOT NULL
            AND EXISTS (SELECT 1 FROM invoicing_deferredsale s WHERE s.purchase_bill_id = NEW.original_document_id
                        AND s.active)
            AND EXISTS (SELECT 1 FROM invoicing_purchasedocumentline l JOIN ledger_account a ON a.id = l.account_id
                        WHERE l.document_id = NEW.id AND a.code = '1250') THEN
        RAISE EXCEPTION 'vendor credit note %: its bill backs a bai'' ajel sale; 1250 is never credited (R124)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS invoicing_bai_ajel_bill_lock ON invoicing_purchasedocument;
CREATE TRIGGER invoicing_bai_ajel_bill_lock BEFORE UPDATE ON invoicing_purchasedocument
    FOR EACH ROW EXECUTE FUNCTION invoicing_bai_ajel_bill_lock();
"""

# Back to the 0029 function (deactivation without the rejected check).
DROP = r"""
DROP TRIGGER IF EXISTS invoicing_bai_ajel_bill_lock ON invoicing_purchasedocument;
DROP FUNCTION IF EXISTS invoicing_bai_ajel_bill_lock();
CREATE OR REPLACE FUNCTION invoicing_deferredsale_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'bai'' ajel sale % is never deleted (R112)', OLD.id;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NOT NEW.active THEN
            RAISE EXCEPTION 'a bai'' ajel sale is created active through invoicing.services (R112)';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF array_length(changed, 1) IS NULL THEN
        RETURN NEW;
    END IF;
    IF NOT changed <@ ARRAY['active'] OR NOT OLD.active OR NEW.active THEN
        RAISE EXCEPTION 'bai'' ajel sale % never changes; only a rejected sale invoice deactivates it (R112)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forwards(apps, schema_editor):
    _run(schema_editor, TRIGGER)


def backwards(apps, schema_editor):
    _run(schema_editor, DROP)


class Migration(migrations.Migration):
    dependencies = [("invoicing", "0030_deferred_sale_base_manager"), ("ledger", "0021_bai_ajel_accounts")]

    operations = [migrations.RunPython(forwards, backwards)]
