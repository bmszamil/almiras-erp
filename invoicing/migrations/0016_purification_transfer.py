"""Purification transfers and payouts (invoicing R54, E14, E15): one document per transfer or payout, numbered PUR.

PostgreSQL lock (the model guards apply on SQLite): created as drafts; draft -> posted only with a number and a
poster; a posted row only gets its entry once; posted rows never change and are never deleted.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


TRIGGER = r"""
CREATE OR REPLACE FUNCTION invoicing_purification_lock() RETURNS trigger AS $$
DECLARE
    changed text[];
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'purification % is %; only drafts are deleted (R54)', OLD.id, OLD.status;
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' OR NEW.number <> '' OR NEW.journal_entry_id IS NOT NULL THEN
            RAISE EXCEPTION 'purification records are created as drafts through invoicing.services';
        END IF;
        RETURN NEW;
    END IF;
    changed := invoicing_changed_keys(to_jsonb(OLD), to_jsonb(NEW));
    IF changed && ARRAY['id', 'entity_id', 'kind', 'created_by_id', 'created_at']
       OR (OLD.number <> '' AND NEW.number <> OLD.number) OR NEW.version < OLD.version THEN
        RAISE EXCEPTION 'purification %: identity, kind, creator and number never change', OLD.id;
    END IF;
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'posted' AND (NEW.number = '' OR NEW.posted_by_id IS NULL OR NEW.journal_entry_id IS NOT NULL) THEN
            RAISE EXCEPTION 'purification % is posted only through invoicing.services (number, poster)', OLD.id;
        END IF;
        IF NEW.status = 'draft' AND changed && ARRAY['number', 'journal_entry_id', 'posted_by_id', 'posted_at'] THEN
            RAISE EXCEPTION 'draft purification %: no number or entry before posting', OLD.id;
        END IF;
    ELSIF NEW.status <> 'posted' OR NOT changed <@ ARRAY['journal_entry_id']
          OR ('journal_entry_id' = ANY(changed) AND OLD.journal_entry_id IS NOT NULL) THEN
        RAISE EXCEPTION 'posted purification % never changes; its entry is recorded once', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER invoicing_purificationtransfer_lock BEFORE INSERT OR UPDATE OR DELETE ON invoicing_purificationtransfer
    FOR EACH ROW EXECUTE FUNCTION invoicing_purification_lock();
"""
DROP = r"""
DROP TRIGGER IF EXISTS invoicing_purificationtransfer_lock ON invoicing_purificationtransfer;
DROP FUNCTION IF EXISTS invoicing_purification_lock();
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

    dependencies = [
        ("core", "0016_seed_access_v7_banking"),
        ("invoicing", "0015_withholding_tax"),
        ("ledger", "0016_wht_accounts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="documentsequence",
            name="doc_type",
            field=models.CharField(
                choices=[
                    ("INV", "Tax invoice / فاتورة ضريبية"),
                    ("CN", "Credit note / إشعار دائن"),
                    ("DN", "Debit note / إشعار مدين"),
                    ("PRE", "Prepayment invoice / فاتورة دفعة مقدمة"),
                    ("BILL", "Vendor bill / فاتورة مورد"),
                    ("VCN", "Vendor credit note / إشعار دائن من مورد"),
                    ("RCPT", "Customer receipt / سند قبض"),
                    ("PAY", "Vendor payment / سند صرف"),
                    (
                        "PUR",
                        "Purification transfer or payout / تحويل أو صرف مبالغ التطهير",
                    ),
                ],
                max_length=4,
                verbose_name="Document type / نوع المستند",
            ),
        ),
        migrations.CreateModel(
            name="PurificationTransfer",
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
                    "updated_at",
                    models.DateTimeField(
                        auto_now=True, verbose_name="Updated at / تاريخ التحديث"
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
                    "source",
                    models.CharField(
                        default="human",
                        help_text="human, import, system:<code> or agent:<name> (D8) / شخص أو استيراد أو نظام أو وكيل ذكاء اصطناعي",
                        max_length=60,
                        verbose_name="Source / المصدر",
                    ),
                ),
                (
                    "version",
                    models.PositiveIntegerField(
                        default=0,
                        editable=False,
                        help_text="Bumped by every edit, line change, submission and return to draft: an approval or posting checked against an older version is refused (access R14, R42) / يزداد مع كل تعديل أو تغيير بند أو تقديم أو إعادة إلى مسودة: يُرفض الاعتماد المبني على نسخة أقدم",
                        verbose_name="Version / رقم النسخة",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            (
                                "transfer",
                                "Transfer to charity payable (E14) / تحويل إلى مستحقات التطهير",
                            ),
                            ("payout", "Payout to charity (E15) / صرف للجهة الخيرية"),
                        ],
                        max_length=8,
                        verbose_name="Kind / النوع",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft / مسودة"),
                            ("posted", "Posted / مرحّل"),
                        ],
                        default="draft",
                        max_length=10,
                        verbose_name="Status / الحالة",
                    ),
                ),
                (
                    "number",
                    models.CharField(
                        blank=True, max_length=40, verbose_name="Number / الرقم"
                    ),
                ),
                ("date", models.DateField(verbose_name="Date / التاريخ")),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2, max_digits=18, verbose_name="Amount / المبلغ"
                    ),
                ),
                (
                    "recipient_en",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="Recipient (English) / الجهة المستفيدة (بالإنجليزية)",
                    ),
                ),
                (
                    "recipient_ar",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="Recipient (Arabic) / الجهة المستفيدة (بالعربية)",
                    ),
                ),
                (
                    "reference_en",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="Reference (English) / المرجع (بالإنجليزية)",
                    ),
                ),
                (
                    "reference_ar",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="Reference (Arabic) / المرجع (بالعربية)",
                    ),
                ),
                (
                    "posted_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Posted at / تاريخ الترحيل"
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
                        verbose_name="Bank account (payout) / الحساب البنكي (الصرف)",
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
                    "entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="core.entity",
                        verbose_name="Entity / الكيان",
                    ),
                ),
                (
                    "journal_entry",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="ledger.journalentry",
                        verbose_name="Journal entry / القيد اليومي",
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
                    "posted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Posted by / رحّله",
                    ),
                ),
            ],
            options={
                "verbose_name": "purification transfer / تحويل مبالغ التطهير",
                "verbose_name_plural": "purification transfers / تحويلات مبالغ التطهير",
                "ordering": ["entity", "-date", "number"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("number", ""), _negated=True),
                        fields=("entity", "number"),
                        name="purification_number_per_entity",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("amount__gt", 0)),
                        name="purification_amount_positive",
                    ),
                ],
            },
        ),
        migrations.RunPython(forwards, backwards),
    ]
