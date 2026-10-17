"""Withholding tax (invoicing D14, R61 to R64): categories (OQ-8 reference rates, UNVERIFIED: tax-adviser sign-off
pending), vendor and bill-line categories, gross-up, treaty certificates and the deduction rows of payments.

The rate list is copied here from ``invoicing.reference_seed.WHT_CATEGORIES_V1`` so this migration keeps doing the
same thing when the seed changes. On PostgreSQL deductions are append-only and verified certificates are locked
(the model guards apply on SQLite).
"""

import core.models
import django.core.validators
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


WHT_VALID_FROM = "2004-07-30"  # placeholder, not verified (reference_seed)
# code, name_en, name_ar, rate, sharia_prohibited
WHT_CATEGORIES = (
    ("management_fees", "Management fees", "أتعاب الإدارة", "20.00", False),
    ("royalties", "Royalties", "الإتاوات", "15.00", False),
    ("dividends", "Dividends", "توزيعات الأرباح", "5.00", False),
    ("rent", "Rent", "الإيجار", "5.00", False),
    ("loan_charges", "Loan charges", "أعباء القروض", "5.00", True),
    ("insurance", "Insurance or reinsurance premiums", "أقساط التأمين أو إعادة التأمين", "5.00", False),
    ("technical_services", "Technical and consulting services", "الخدمات الفنية والاستشارية", "5.00", False),
    ("air_freight", "Air tickets, air or sea freight", "تذاكر الطيران والشحن الجوي أو البحري", "5.00", False),
    ("international_telecom", "International telecommunication services", "خدمات الاتصالات الدولية", "5.00", False),
    ("land_transport", "Land transport within KSA", "النقل البري داخل المملكة", "15.00", False),
    ("other_payments", "Any other payments", "أي دفعات أخرى", "15.00", False),
)


def load_categories(apps, schema_editor):
    from datetime import date
    from decimal import Decimal

    WhtCategory = apps.get_model("invoicing", "WhtCategory")
    for code, en, ar, rate, prohibited in WHT_CATEGORIES:
        WhtCategory.objects.get_or_create(
            code=code, valid_from=date.fromisoformat(WHT_VALID_FROM),
            defaults=dict(name_en=en, name_ar=ar, rate=Decimal(rate), is_verified=False, sharia_prohibited=prohibited),
        )


TRIGGERS = r"""
CREATE OR REPLACE FUNCTION invoicing_whtdeduction_lock() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'withholding tax deduction % is never changed or deleted (R62, R64)', OLD.id;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER invoicing_whtdeduction_lock BEFORE UPDATE OR DELETE ON invoicing_whtdeduction
    FOR EACH ROW EXECUTE FUNCTION invoicing_whtdeduction_lock();

CREATE OR REPLACE FUNCTION invoicing_treatycertificate_lock() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'treaty certificate % is never deleted', OLD.id;
    END IF;
    IF OLD.verified_at IS NOT NULL THEN
        RAISE EXCEPTION 'treaty certificate % is verified; it is never edited (enter a new one)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER invoicing_treatycertificate_lock BEFORE UPDATE OR DELETE ON invoicing_treatycertificate
    FOR EACH ROW EXECUTE FUNCTION invoicing_treatycertificate_lock();
"""
DROP = r"""
DROP TRIGGER IF EXISTS invoicing_whtdeduction_lock ON invoicing_whtdeduction;
DROP TRIGGER IF EXISTS invoicing_treatycertificate_lock ON invoicing_treatycertificate;
DROP FUNCTION IF EXISTS invoicing_whtdeduction_lock();
DROP FUNCTION IF EXISTS invoicing_treatycertificate_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def triggers(apps, schema_editor):
    _run(schema_editor, TRIGGERS)


def drop_triggers(apps, schema_editor):
    _run(schema_editor, DROP)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_seed_access_v7_banking"),
        ("invoicing", "0014_billing_links"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="wht_gross_up",
            field=models.BooleanField(
                default=False,
                help_text="R63: the vendor receives the full amount; method pending tax sign-off / يستلم المورد المبلغ كاملًا؛ الطريقة بانتظار اعتماد المستشار الضريبي",
                verbose_name="Entity bears the withholding tax (gross-up) / تتحمل المنشأة ضريبة الاستقطاع",
            ),
        ),
        migrations.AddField(
            model_name="vendorpayment",
            name="wht_amount",
            field=models.DecimalField(
                decimal_places=3,
                default=Decimal("0"),
                max_digits=21,
                verbose_name="Withholding tax / ضريبة الاستقطاع",
            ),
        ),
        migrations.AddField(
            model_name="vendorpayment",
            name="wht_gross_up",
            field=models.BooleanField(
                default=False,
                verbose_name="Withholding tax borne by the entity / الضريبة تتحملها المنشأة",
            ),
        ),
        migrations.CreateModel(
            name="WhtCategory",
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
                ("code", models.SlugField(max_length=40, verbose_name="Code / الرمز")),
                (
                    "name_en",
                    models.CharField(
                        max_length=200,
                        verbose_name="Name (English) / الاسم (بالإنجليزية)",
                    ),
                ),
                (
                    "name_ar",
                    models.CharField(
                        max_length=200, verbose_name="Name (Arabic) / الاسم (بالعربية)"
                    ),
                ),
                (
                    "rate",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=5,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0"))
                        ],
                        verbose_name="Rate % / النسبة %",
                    ),
                ),
                ("valid_from", models.DateField(verbose_name="Valid from / ساري من")),
                (
                    "valid_to",
                    models.DateField(
                        blank=True, null=True, verbose_name="Valid to / ساري حتى"
                    ),
                ),
                (
                    "return_line_code",
                    models.CharField(
                        blank=True,
                        max_length=20,
                        verbose_name="WHT return line / بند إقرار الاستقطاع",
                    ),
                ),
                (
                    "is_verified",
                    models.BooleanField(
                        default=False,
                        help_text="Reference data until the tax adviser signs off (OQ-8) / بيانات مرجعية حتى اعتماد المستشار الضريبي",
                        verbose_name="Verified by the tax adviser / معتمد من المستشار الضريبي",
                    ),
                ),
                (
                    "sharia_prohibited",
                    models.BooleanField(
                        default=False,
                        help_text="Loan charges never arise in a Sharia-enforcing entity (OQ-8, R5, R7) / لا تنشأ أعباء القروض في كيان يطبق الضوابط الشرعية",
                        verbose_name="Not allowed where Sharia is enforced / غير مسموح عند تطبيق الضوابط الشرعية",
                    ),
                ),
            ],
            options={
                "verbose_name": "withholding tax category / فئة ضريبة الاستقطاع",
                "verbose_name_plural": "withholding tax categories / فئات ضريبة الاستقطاع",
                "ordering": ["code", "valid_from"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("code", "valid_from"), name="wht_category_one_per_start"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("rate__gte", 0), ("rate__lt", 100)),
                        name="wht_rate_range",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="TreatyCertificate",
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
                    "country",
                    models.CharField(
                        max_length=2,
                        validators=[core.models.validate_country_code],
                        verbose_name="Treaty country / دولة الاتفاقية",
                    ),
                ),
                (
                    "treaty_rate",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=5,
                        validators=[
                            django.core.validators.MinValueValidator(Decimal("0"))
                        ],
                        verbose_name="Treaty rate % / نسبة الاتفاقية %",
                    ),
                ),
                ("valid_from", models.DateField(verbose_name="Valid from / ساري من")),
                ("valid_to", models.DateField(verbose_name="Valid to / ساري حتى")),
                (
                    "document",
                    models.FileField(
                        blank=True,
                        upload_to="invoicing/treaty/%Y/",
                        verbose_name="Certificate / الشهادة",
                    ),
                ),
                (
                    "received_on",
                    models.DateField(verbose_name="Received on / تاريخ الاستلام"),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Entered at / تاريخ الإدخال"
                    ),
                ),
                (
                    "verified_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Verified at / تاريخ التحقق"
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Entered by / أدخله",
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
                    "vendor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="treaty_certificates",
                        to="invoicing.vendor",
                        verbose_name="Vendor / المورد",
                    ),
                ),
                (
                    "verified_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Verified by / تحقق منه",
                    ),
                ),
                (
                    "wht_category",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="invoicing.whtcategory",
                        verbose_name="Category (blank = all) / الفئة (فارغ = الكل)",
                    ),
                ),
            ],
            options={
                "verbose_name": "treaty certificate / شهادة اتفاقية ضريبية",
                "verbose_name_plural": "treaty certificates / شهادات الاتفاقيات الضريبية",
            },
        ),
        migrations.AddField(
            model_name="purchasedocumentline",
            name="wht_category",
            field=models.ForeignKey(
                blank=True,
                help_text="Required to pay a non-resident vendor (R61); defaults to the vendor's / مطلوبة للدفع لمورد غير مقيم؛ الافتراضي فئة المورد",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="invoicing.whtcategory",
                verbose_name="Withholding tax category / فئة ضريبة الاستقطاع",
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="wht_category",
            field=models.ForeignKey(
                blank=True,
                help_text="Non-resident vendors: default for bill lines; payments need it (R61) / للموردين غير المقيمين: الافتراضي لبنود الفواتير، وتحتاجه المدفوعات",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="invoicing.whtcategory",
                verbose_name="Withholding tax category / فئة ضريبة الاستقطاع",
            ),
        ),
        migrations.CreateModel(
            name="WhtDeduction",
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
                    "base_amount",
                    models.DecimalField(
                        decimal_places=3, max_digits=21, verbose_name="Base / الوعاء"
                    ),
                ),
                (
                    "base_amount_sar",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Base in SAR / الوعاء بالريال",
                    ),
                ),
                (
                    "rate_applied",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=5,
                        verbose_name="Rate applied % / النسبة المطبقة %",
                    ),
                ),
                (
                    "wht_amount",
                    models.DecimalField(
                        decimal_places=3,
                        max_digits=21,
                        verbose_name="Withholding tax / ضريبة الاستقطاع",
                    ),
                ),
                (
                    "wht_amount_sar",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Withholding tax in SAR / ضريبة الاستقطاع بالريال",
                    ),
                ),
                (
                    "gross_up",
                    models.BooleanField(
                        default=False,
                        verbose_name="Borne by the entity / تتحملها المنشأة",
                    ),
                ),
                (
                    "return_month",
                    models.DateField(verbose_name="Return month / شهر الإقرار"),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Created at / تاريخ الإنشاء"
                    ),
                ),
                (
                    "bill",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="invoicing.purchasedocument",
                        verbose_name="Bill / الفاتورة",
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
                    "payment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wht_deductions",
                        to="invoicing.vendorpayment",
                        verbose_name="Payment / سند الصرف",
                    ),
                ),
                (
                    "treaty_certificate",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="invoicing.treatycertificate",
                        verbose_name="Treaty certificate / شهادة الاتفاقية",
                    ),
                ),
                (
                    "wht_category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="invoicing.whtcategory",
                        verbose_name="Category / الفئة",
                    ),
                ),
            ],
            options={
                "verbose_name": "withholding tax deduction / استقطاع ضريبي",
                "verbose_name_plural": "withholding tax deductions / استقطاعات ضريبية",
                "ordering": ["entity", "return_month", "pk"],
            },
        ),
        migrations.AddConstraint(
            model_name="treatycertificate",
            constraint=models.CheckConstraint(
                condition=models.Q(("treaty_rate__gte", 0), ("treaty_rate__lt", 100)),
                name="treaty_rate_range",
            ),
        ),
        migrations.AddConstraint(
            model_name="treatycertificate",
            constraint=models.CheckConstraint(
                condition=models.Q(("valid_to__gte", models.F("valid_from"))),
                name="treaty_dates",
            ),
        ),
        migrations.AddConstraint(
            model_name="whtdeduction",
            constraint=models.UniqueConstraint(
                fields=("payment", "bill", "wht_category"),
                name="one_wht_row_per_category",
            ),
        ),
        migrations.RunPython(load_categories, migrations.RunPython.noop),
        migrations.RunPython(triggers, drop_triggers),
    ]
