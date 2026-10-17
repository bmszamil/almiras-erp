"""PDPL anonymisation (invoicing R52) and billing links in the sales-document lock (PostgreSQL only).

Replaces ``invoicing_salesdocument_lock`` (migration 0009): ``billing_item`` and ``billing_adjustment`` never change
(R60, R73); a final or rejected document changes once more only for the anonymisation job: after its
``retention_until``, only the individual buyer's snapshot fields and ``anonymised_at``. Everything else is as in 0009.
"""

from django.db import migrations, models


FORWARD = r"""
CREATE OR REPLACE FUNCTION invoicing_salesdocument_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    zatca text[] := ARRAY['zatca_status', 'zatca_icv', 'zatca_previous_hash', 'zatca_hash', 'zatca_qr',
                          'zatca_messages'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'sales document % is %; only drafts are deleted (R13)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL
           OR NEW.zatca_status <> 'not_submitted' THEN
            RAISE EXCEPTION 'sales documents are created as drafts through invoicing.services';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'uuid', 'entity_id', 'doc_type', 'created_by_id', 'created_at', 'billing_item_id',
                        'billing_adjustment_id']
       OR (OLD.number <> '' AND NEW.number <> OLD.number) THEN
        RAISE EXCEPTION 'sales document %: identity, type, creator, billing link and number never change', OLD.id;
    END IF;
    IF 'anonymised_at' = ANY(changed) AND OLD.status NOT IN ('final', 'rejected') THEN
        RAISE EXCEPTION 'sales document %: only final or rejected documents are anonymised (R52)', OLD.id;
    END IF;
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION '% %: the version only goes up', 'sales document', OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status NOT IN ('draft', 'submitted')
           OR changed && (zatca || ARRAY['number', 'journal_entry_id', 'approved_by_id', 'approved_at']) THEN
            RAISE EXCEPTION 'draft sales document %: only edits and submission (R57)', OLD.id;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'draft' THEN
            IF NOT changed <@ ARRAY['status', 'modified_at', 'modified_by_id', 'version'] THEN
                RAISE EXCEPTION 'sales document %: a return to draft changes nothing else', OLD.id;
            END IF;
        ELSIF NEW.status = 'issued' THEN
            IF NEW.number = '' OR NEW.approved_by_id IS NULL OR NEW.approved_at IS NULL OR NEW.issue_time IS NULL
               OR changed && (zatca || ARRAY['journal_entry_id']) THEN
                RAISE EXCEPTION 'sales document % is issued only by approval (number, approver, time)', OLD.id;
            END IF;
        ELSIF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'submitted sales document % changes only by approval or return to draft', OLD.id;
        END IF;
    ELSIF OLD.status = 'issued' THEN
        IF NEW.status NOT IN ('issued', 'final', 'rejected')
           OR NOT changed <@ (zatca || ARRAY['status', 'journal_entry_id']) THEN
            RAISE EXCEPTION 'issued sales document % changes only through the ZATCA result and its posting (R45)', OLD.id;
        END IF;
        IF 'journal_entry_id' = ANY(changed) AND (OLD.journal_entry_id IS NOT NULL OR NEW.status <> 'final') THEN
            RAISE EXCEPTION 'sales document %: its entry is set once, when it becomes final', OLD.id;
        END IF;
        IF NEW.status = 'final' AND NOT EXISTS (
            SELECT 1 FROM ledger_journalentry e
             WHERE e.id = NEW.journal_entry_id AND e.status = 'posted' AND e.entity_id = NEW.entity_id
               AND e.document_type = 'invoicing.salesdocument' AND e.document_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'sales document % is final only with its own posted entry (R41)', OLD.id;
        END IF;
        IF NEW.status = 'final' AND NEW.zatca_status NOT IN ('cleared', 'reported', 'cleared_with_warnings',
                                                              'reported_with_warnings') THEN
            RAISE EXCEPTION 'sales document % is final only once ZATCA cleared or reported it (R44)', OLD.id;
        END IF;
        IF NEW.status = 'rejected' AND (NEW.zatca_status <> 'rejected' OR NEW.journal_entry_id IS NOT NULL) THEN
            RAISE EXCEPTION 'sales document % is rejected only by a ZATCA rejection, with no entry (R42)', OLD.id;
        END IF;
    ELSIF array_length(changed, 1) IS NOT NULL THEN
        -- R52: the anonymisation job, once, after retention_until, only the individual buyer's snapshot.
        IF OLD.anonymised_at IS NULL AND NEW.anonymised_at IS NOT NULL AND OLD.retention_until IS NOT NULL
           AND OLD.retention_until < (NEW.anonymised_at AT TIME ZONE 'Asia/Riyadh')::date
           AND changed <@ ARRAY['anonymised_at', 'buyer_name_en', 'buyer_name_ar', 'buyer_street_en',
                                'buyer_street_ar', 'buyer_district_en', 'buyer_district_ar', 'buyer_city_en',
                                'buyer_city_ar', 'buyer_vat_number', 'buyer_other_id', 'buyer_building_no',
                                'buyer_additional_no', 'buyer_postal_code'] THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'sales document % is %; it never changes (R13)', OLD.id, OLD.status;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

"""

PREVIOUS = r"""
CREATE OR REPLACE FUNCTION invoicing_salesdocument_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
    zatca text[] := ARRAY['zatca_status', 'zatca_icv', 'zatca_previous_hash', 'zatca_hash', 'zatca_qr',
                          'zatca_messages'];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'sales document % is %; only drafts are deleted (R13)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL
           OR NEW.zatca_status <> 'not_submitted' THEN
            RAISE EXCEPTION 'sales documents are created as drafts through invoicing.services';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'uuid', 'entity_id', 'doc_type', 'created_by_id', 'created_at']
       OR (OLD.number <> '' AND NEW.number <> OLD.number) THEN
        RAISE EXCEPTION 'sales document %: identity, type, creator and number never change', OLD.id;
    END IF;
    IF NEW.version < OLD.version THEN
        RAISE EXCEPTION '% %: the version only goes up', 'sales document', OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status NOT IN ('draft', 'submitted')
           OR changed && (zatca || ARRAY['number', 'journal_entry_id', 'approved_by_id', 'approved_at']) THEN
            RAISE EXCEPTION 'draft sales document %: only edits and submission (R57)', OLD.id;
        END IF;
    ELSIF OLD.status = 'submitted' THEN
        IF NEW.status = 'draft' THEN
            IF NOT changed <@ ARRAY['status', 'modified_at', 'modified_by_id', 'version'] THEN
                RAISE EXCEPTION 'sales document %: a return to draft changes nothing else', OLD.id;
            END IF;
        ELSIF NEW.status = 'issued' THEN
            IF NEW.number = '' OR NEW.approved_by_id IS NULL OR NEW.approved_at IS NULL OR NEW.issue_time IS NULL
               OR changed && (zatca || ARRAY['journal_entry_id']) THEN
                RAISE EXCEPTION 'sales document % is issued only by approval (number, approver, time)', OLD.id;
            END IF;
        ELSIF array_length(changed, 1) IS NOT NULL THEN
            RAISE EXCEPTION 'submitted sales document % changes only by approval or return to draft', OLD.id;
        END IF;
    ELSIF OLD.status = 'issued' THEN
        IF NEW.status NOT IN ('issued', 'final', 'rejected')
           OR NOT changed <@ (zatca || ARRAY['status', 'journal_entry_id']) THEN
            RAISE EXCEPTION 'issued sales document % changes only through the ZATCA result and its posting (R45)', OLD.id;
        END IF;
        IF 'journal_entry_id' = ANY(changed) AND (OLD.journal_entry_id IS NOT NULL OR NEW.status <> 'final') THEN
            RAISE EXCEPTION 'sales document %: its entry is set once, when it becomes final', OLD.id;
        END IF;
        IF NEW.status = 'final' AND NOT EXISTS (
            SELECT 1 FROM ledger_journalentry e
             WHERE e.id = NEW.journal_entry_id AND e.status = 'posted' AND e.entity_id = NEW.entity_id
               AND e.document_type = 'invoicing.salesdocument' AND e.document_id = NEW.id
        ) THEN
            RAISE EXCEPTION 'sales document % is final only with its own posted entry (R41)', OLD.id;
        END IF;
        IF NEW.status = 'final' AND NEW.zatca_status NOT IN ('cleared', 'reported', 'cleared_with_warnings',
                                                              'reported_with_warnings') THEN
            RAISE EXCEPTION 'sales document % is final only once ZATCA cleared or reported it (R44)', OLD.id;
        END IF;
        IF NEW.status = 'rejected' AND (NEW.zatca_status <> 'rejected' OR NEW.journal_entry_id IS NOT NULL) THEN
            RAISE EXCEPTION 'sales document % is rejected only by a ZATCA rejection, with no entry (R42)', OLD.id;
        END IF;
    ELSIF array_length(changed, 1) IS NOT NULL THEN
        RAISE EXCEPTION 'sales document % is %; it never changes (R13)', OLD.id, OLD.status;
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
    _run(schema_editor, FORWARD)


def backwards(apps, schema_editor):
    _run(schema_editor, PREVIOUS)


class Migration(migrations.Migration):

    dependencies = [
        ("invoicing", "0016_purification_transfer"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="anonymised_at",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text="R52: personal data replaced by a fixed token after the retention period / استُبدلت البيانات الشخصية برمز ثابت بعد انتهاء مدة الحفظ",
                null=True,
                verbose_name="Anonymised at / تاريخ إخفاء الهوية",
            ),
        ),
        migrations.AddField(
            model_name="salesdocument",
            name="anonymised_at",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text="R52: the individual buyer's snapshot replaced by a fixed token after retention_until; amounts, VAT, number and ZATCA data kept / استُبدلت بيانات المشتري الفرد برمز ثابت بعد انتهاء مدة الحفظ",
                null=True,
                verbose_name="Buyer data anonymised at / تاريخ إخفاء هوية المشتري",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
