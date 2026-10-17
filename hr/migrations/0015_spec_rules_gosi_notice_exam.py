"""hr spec rules the review found missing (finding 11): exam result (R33), Art. 79 bis postponement by the approver
and reply reminders, the GOSI exclusion fields and checklist (R65), and pregnancy notices without leave (R60)."""

import django.db.models.deletion
import hr.crypto
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0016_seed_access_v7_banking"),
        ("hr", "0014_outbox_actions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="leaverequest",
            name="exam_taken",
            field=models.BooleanField(
                blank=True, null=True, verbose_name="Exam taken / أُدّي الامتحان"
            ),
        ),
        migrations.AddField(
            model_name="termination",
            name="deadline_alerts",
            field=models.JSONField(
                blank=True,
                default=dict,
                verbose_name="Reply reminders sent / تذكيرات الرد المرسلة",
            ),
        ),
        migrations.AddField(
            model_name="termination",
            name="gosi_excluded_on",
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name="Excluded on GOSI on / تاريخ الاستبعاد من التأمينات",
            ),
        ),
        migrations.AddField(
            model_name="termination",
            name="gosi_exclusion_reason",
            field=models.CharField(
                blank=True,
                max_length=40,
                verbose_name="GOSI exclusion reason / سبب الاستبعاد من التأمينات",
            ),
        ),
        migrations.AddField(
            model_name="termination",
            name="gosi_exclusion_ref",
            field=models.CharField(
                blank=True,
                max_length=60,
                verbose_name="GOSI portal reference / مرجع بوابة التأمينات",
            ),
        ),
        migrations.AddField(
            model_name="termination",
            name="postponed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Postponed by / أجّله",
            ),
        ),
        migrations.AddField(
            model_name="termination",
            name="postponement_reason_en",
            field=models.TextField(
                blank=True,
                verbose_name="Postponement reason (English) / سبب التأجيل (بالإنجليزية)",
            ),
        ),
        migrations.CreateModel(
            name="HrChecklistItem",
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
                    "kind",
                    models.CharField(
                        choices=[
                            (
                                "gosi_exclusion",
                                "Exclude on GOSI / الاستبعاد من التأمينات",
                            )
                        ],
                        max_length=20,
                        verbose_name="Kind / النوع",
                    ),
                ),
                ("due_date", models.DateField(verbose_name="Due by / موعد الإنجاز")),
                (
                    "text_en",
                    models.TextField(
                        verbose_name="Text (English) / النص (بالإنجليزية)"
                    ),
                ),
                (
                    "text_ar",
                    models.TextField(verbose_name="Text (Arabic) / النص (بالعربية)"),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Open / مفتوح"), ("closed", "Closed / مغلق")],
                        default="open",
                        max_length=10,
                        verbose_name="Status / الحالة",
                    ),
                ),
                (
                    "reminders_sent",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        verbose_name="Reminders sent / التذكيرات المرسلة",
                    ),
                ),
                (
                    "closed_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Closed at / تاريخ الإغلاق"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        auto_now_add=True, verbose_name="Created at / تاريخ الإنشاء"
                    ),
                ),
                (
                    "closed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Closed by / أغلقه",
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
                    "termination",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="checklist_items",
                        to="hr.termination",
                        verbose_name="Termination / إنهاء الخدمة",
                    ),
                ),
            ],
            options={
                "verbose_name": "HR checklist item / بند قائمة مهام الموارد البشرية",
                "verbose_name_plural": "HR checklist items / قائمة مهام الموارد البشرية",
                "ordering": ["due_date", "id"],
            },
        ),
        migrations.CreateModel(
            name="PregnancyNotice",
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
                    "source",
                    models.CharField(
                        choices=[
                            ("human", "Person / شخص"),
                            ("import", "Import / استيراد"),
                            ("system", "System / النظام"),
                        ],
                        default="human",
                        max_length=10,
                        verbose_name="Source / المصدر",
                    ),
                ),
                (
                    "document_ref",
                    hr.crypto.EncryptedTextField(
                        plain_max_length=100,
                        verbose_name="Document reference / مرجع المستند",
                    ),
                ),
                (
                    "notified_on",
                    models.DateField(verbose_name="Notified on / تاريخ الإبلاغ"),
                ),
                (
                    "ended_on",
                    models.DateField(
                        blank=True, null=True, verbose_name="Ended on / تاريخ الانتهاء"
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
            ],
            options={
                "verbose_name": "pregnancy notice / إشعار حمل",
                "verbose_name_plural": "pregnancy notices / إشعارات الحمل",
                "ordering": ["employee", "notified_on"],
            },
        ),
    ]
