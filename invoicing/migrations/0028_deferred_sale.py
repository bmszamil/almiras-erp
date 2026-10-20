"""R112 (bai' ajel sale to an employee; payroll R47, owner decision E8): the employee's own customer record
(``Customer.employee``), the sale (``DeferredSale``) and the profit recognised as instalments are collected
(``DeferredProfitRelease``). Reference data and PostgreSQL guards: migration 0029."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_seed_access_v15_deferred_sale"),
        ("hr", "0020_settlement_notice_shortfall_wage"),
        ("invoicing", "0027_b2g_payment_order_tax_point"),
        ("ledger", "0021_bai_ajel_accounts"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DeferredProfitRelease",
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
                    "source_ref",
                    models.CharField(
                        max_length=100,
                        verbose_name="Collection (payroll) / التحصيل (الرواتب)",
                    ),
                ),
                ("date", models.DateField(verbose_name="Date / التاريخ")),
                (
                    "collected_amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Amount collected / المبلغ المحصل",
                    ),
                ),
                (
                    "collected_to_date",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Collected to date / المحصل حتى تاريخه",
                    ),
                ),
                (
                    "profit_amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Profit recognised / الربح المحقق",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("posted", "Posted / مرحّل"),
                            ("reversed", "Reversed / معكوس"),
                        ],
                        default="posted",
                        max_length=10,
                        verbose_name="Status / الحالة",
                    ),
                ),
                (
                    "reversed_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Reversed at / تاريخ العكس"
                    ),
                ),
                (
                    "reversal_reason",
                    models.CharField(
                        blank=True,
                        max_length=300,
                        verbose_name="Reversal reason / سبب العكس",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Created at / تاريخ الإنشاء"
                    ),
                ),
            ],
            options={
                "verbose_name": "bai' ajel profit recognition / تحقق أرباح البيع الآجل",
                "verbose_name_plural": "bai' ajel profit recognitions / تحقق أرباح البيع الآجل",
                "ordering": ["deferred_sale", "date", "id"],
            },
        ),
        migrations.CreateModel(
            name="DeferredSale",
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
                    "source_type",
                    models.CharField(
                        help_text="app_label.model of the approved contract (payroll) / نوع العقد المعتمد (الرواتب)",
                        max_length=60,
                        verbose_name="Approved in / اعتُمد في",
                    ),
                ),
                (
                    "source_id",
                    models.PositiveBigIntegerField(verbose_name="Contract / العقد"),
                ),
                (
                    "cost",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Cost (from 1250) / التكلفة (من 1250)",
                    ),
                ),
                (
                    "profit_amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="Fixed profit / الربح الثابت",
                    ),
                ),
                (
                    "vat_amount",
                    models.DecimalField(
                        decimal_places=2,
                        max_digits=18,
                        verbose_name="VAT charged to the employee / الضريبة على الموظف",
                    ),
                ),
                (
                    "possession_date",
                    models.DateField(verbose_name="Possession date / تاريخ القبض"),
                ),
                (
                    "possession_evidence_ref",
                    models.CharField(
                        max_length=200, verbose_name="Possession evidence / مستند القبض"
                    ),
                ),
                (
                    "approved_at",
                    models.DateTimeField(
                        verbose_name="Sale approved at / تاريخ اعتماد البيع"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Created at / تاريخ الإنشاء"
                    ),
                ),
                (
                    "active",
                    models.BooleanField(
                        default=True,
                        help_text="False once ZATCA rejected the sale invoice and a new one replaced it / يصبح غير نشط إذا رفضت الهيئة فاتورة البيع واستُبدلت بأخرى",
                        verbose_name="Active / نشط",
                    ),
                ),
            ],
            options={
                "verbose_name": "bai' ajel sale / بيع آجل",
                "verbose_name_plural": "bai' ajel sales / عمليات البيع الآجل",
                "ordering": ["entity", "-created_at"],
            },
        ),
        migrations.AddField(
            model_name="customer",
            name="employee",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                help_text="The employee this individual customer is (bai' ajel sale, R112) / الموظف الذي يمثله هذا العميل الفرد (البيع الآجل)",
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="hr.employee",
                verbose_name="Employee / الموظف",
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.UniqueConstraint(
                condition=models.Q(("employee__isnull", False)),
                fields=("entity", "employee"),
                name="one_customer_per_employee",
            ),
        ),
        migrations.AddField(
            model_name="deferredprofitrelease",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Collected by (payroll approver) / حصّله (معتمد الرواتب)",
            ),
        ),
        migrations.AddField(
            model_name="deferredprofitrelease",
            name="entity",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.entity",
                verbose_name="Entity / الكيان",
            ),
        ),
        migrations.AddField(
            model_name="deferredprofitrelease",
            name="journal_entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.journalentry",
                verbose_name="Journal entry / القيد اليومي",
            ),
        ),
        migrations.AddField(
            model_name="deferredprofitrelease",
            name="reversal_entry",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="ledger.journalentry",
                verbose_name="Reversal entry / قيد العكس",
            ),
        ),
        migrations.AddField(
            model_name="deferredprofitrelease",
            name="reversed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Reversed by / عكسه",
            ),
        ),
        migrations.AddField(
            model_name="deferredsale",
            name="approved_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Sale approved by / اعتمد البيع",
            ),
        ),
        migrations.AddField(
            model_name="deferredsale",
            name="created_by",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Created by / أنشأه",
            ),
        ),
        migrations.AddField(
            model_name="deferredsale",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="invoicing.customer",
                verbose_name="Employee (customer) / الموظف (العميل)",
            ),
        ),
        migrations.AddField(
            model_name="deferredsale",
            name="entity",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="core.entity",
                verbose_name="Entity / الكيان",
            ),
        ),
        migrations.AddField(
            model_name="deferredsale",
            name="purchase_bill",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="invoicing.purchasedocument",
                verbose_name="Purchase bill (1250) / فاتورة الشراء (1250)",
            ),
        ),
        migrations.AddField(
            model_name="deferredsale",
            name="sales_document",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deferred_sale",
                to="invoicing.salesdocument",
                verbose_name="Sale invoice / فاتورة البيع",
            ),
        ),
        migrations.AddField(
            model_name="deferredprofitrelease",
            name="deferred_sale",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="releases",
                to="invoicing.deferredsale",
                verbose_name="Bai' ajel sale / البيع الآجل",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsale",
            constraint=models.UniqueConstraint(
                condition=models.Q(("active", True)),
                fields=("purchase_bill",),
                name="deferred_sale_one_per_bill",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsale",
            constraint=models.UniqueConstraint(
                condition=models.Q(("active", True)),
                fields=("entity", "source_type", "source_id"),
                name="deferred_sale_one_per_contract",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsale",
            constraint=models.CheckConstraint(
                condition=models.Q(("cost__gt", 0)), name="deferred_sale_cost_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsale",
            constraint=models.CheckConstraint(
                condition=models.Q(("profit_amount__gte", 0)),
                name="deferred_sale_profit_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredsale",
            constraint=models.CheckConstraint(
                condition=models.Q(("vat_amount__gte", 0)),
                name="deferred_sale_vat_not_negative",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredprofitrelease",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "posted")),
                fields=("deferred_sale", "source_ref"),
                name="deferred_profit_one_per_collection",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredprofitrelease",
            constraint=models.CheckConstraint(
                condition=models.Q(("collected_amount__gt", 0)),
                name="deferred_profit_collected_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="deferredprofitrelease",
            constraint=models.CheckConstraint(
                condition=models.Q(("profit_amount__gte", 0)),
                name="deferred_profit_not_negative",
            ),
        ),
    ]
