"""hr R25, R41 (review finding 10, nit 16): superseded outbox events and the log of re-queue and supersede
actions; events linked to the termination that emitted them; voided EOSB snapshots of withdrawn terminations."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_seed_access_v7_banking"),
        ("hr", "0013_employee_gosi_record"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="assignmentevent",
            name="termination",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="assignment_events",
                to="hr.termination",
                verbose_name="Termination / إنهاء الخدمة",
            ),
        ),
        migrations.AddField(
            model_name="eosbcalculation",
            name="voided",
            field=models.BooleanField(
                default=False,
                verbose_name="Voided (termination withdrawn) / ملغاة (سُحب إنهاء الخدمة)",
            ),
        ),
        migrations.AlterField(
            model_name="assignmentevent",
            name="delivery_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending / قيد الانتظار"),
                    ("delivered", "Delivered / تم التسليم"),
                    ("failed", "Failed / فشل"),
                    ("superseded", "Superseded / مستبدل"),
                ],
                default="pending",
                max_length=10,
                verbose_name="Delivery / التسليم",
            ),
        ),
        migrations.AlterField(
            model_name="hralert",
            name="kind",
            field=models.CharField(
                choices=[
                    ("document_expiry", "Document expiry / انتهاء مستند"),
                    (
                        "billing_delivery_failed",
                        "Billing delivery failed / تعذر التسليم للفوترة",
                    ),
                    (
                        "billing_delivery_blocked",
                        "Billing delivery blocked / تسليم للفوترة متوقف",
                    ),
                    (
                        "resignation_deadline",
                        "Resignation reply due / موعد الرد على الاستقالة",
                    ),
                    (
                        "gosi_exclusion",
                        "GOSI exclusion due / موعد الاستبعاد من التأمينات",
                    ),
                ],
                max_length=30,
                verbose_name="Kind / النوع",
            ),
        ),
        migrations.CreateModel(
            name="AssignmentEventAction",
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
                    "action",
                    models.CharField(
                        choices=[
                            ("requeue", "Re-queued / أعيد إلى الانتظار"),
                            ("supersede", "Superseded / استُبدل"),
                        ],
                        max_length=10,
                        verbose_name="Action / الإجراء",
                    ),
                ),
                (
                    "attempts_before",
                    models.PositiveSmallIntegerField(
                        verbose_name="Attempts before / المحاولات قبل الإجراء"
                    ),
                ),
                (
                    "last_error_en",
                    models.TextField(
                        blank=True,
                        verbose_name="Last error (English) / آخر خطأ (بالإنجليزية)",
                    ),
                ),
                (
                    "last_error_ar",
                    models.TextField(
                        blank=True,
                        verbose_name="Last error (Arabic) / آخر خطأ (بالعربية)",
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
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Created at / تاريخ الإنشاء"
                    ),
                ),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Actor / المنفّذ",
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
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="actions",
                        to="hr.assignmentevent",
                        verbose_name="Event / الحدث",
                    ),
                ),
            ],
            options={
                "verbose_name": "assignment event action / إجراء على حدث تكليف",
                "verbose_name_plural": "assignment event actions / إجراءات أحداث التكليفات",
                "ordering": ["id"],
            },
        ),
    ]
