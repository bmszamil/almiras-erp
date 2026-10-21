"""zatca review 2026-10-21 of the D-19 branch:

  * ``OnboardingEvent.secret_ref`` and a partial unique constraint: at most one ``secret_version_retired`` per
    entity and secret-store version (item 9); action ``secret_retirement_requested`` (item 3: after commit the
    web process only records the request, the scheduler's retirement job retires);
  * ``EgsUnit.renewal_started_at``: one renewal at a time per unit (item 8);
  * PostgreSQL trigger (SQLite does nothing): ``attention_reason`` is set only right after a
    ``renewal_needs_attention`` event of the unit, and cleared only by a revocation, a renewal (new private-key
    reference) or right after an ``attention_cleared`` event (item 7).
"""
from django.db import migrations, models

ATTENTION_LOCK = r"""
CREATE OR REPLACE FUNCTION zatca_egsunit_attention_lock() RETURNS trigger AS $$
DECLARE
    latest text;
BEGIN
    IF NEW.attention_reason IS NOT DISTINCT FROM OLD.attention_reason THEN
        RETURN NEW;
    END IF;
    SELECT action INTO latest FROM zatca_onboardingevent
     WHERE egs_unit_id = NEW.id AND action IN ('renewal_needs_attention', 'attention_cleared')
     ORDER BY id DESC LIMIT 1;
    IF NEW.attention_reason <> '' THEN
        IF latest IS DISTINCT FROM 'renewal_needs_attention' THEN
            RAISE EXCEPTION 'EGS unit %: needs attention only with a renewal_needs_attention event (R-10)', OLD.id;
        END IF;
    ELSIF NOT (NEW.status = 'revoked' OR NEW.private_key_ref IS DISTINCT FROM OLD.private_key_ref
               OR latest = 'attention_cleared') THEN
        RAISE EXCEPTION 'EGS unit %: attention is cleared only with an attention_cleared event, a renewal or a '
                        'revocation (R-10)', OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER zatca_egsunit_attention_lock BEFORE UPDATE ON zatca_egsunit
    FOR EACH ROW EXECUTE FUNCTION zatca_egsunit_attention_lock();
"""

DROP_ATTENTION_LOCK = r"""
DROP TRIGGER IF EXISTS zatca_egsunit_attention_lock ON zatca_egsunit;
DROP FUNCTION IF EXISTS zatca_egsunit_attention_lock();
"""


def _run(schema_editor, sql):
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(sql)  # no parameters: the "%" in RAISE messages is not a placeholder


def forward(apps, schema_editor):
    _run(schema_editor, ATTENTION_LOCK)


def reverse(apps, schema_editor):
    _run(schema_editor, DROP_ATTENTION_LOCK)


class Migration(migrations.Migration):

    dependencies = [
        ("zatca", "0005_egs_attention_and_retirement_events"),
    ]

    operations = [
        migrations.AddField(
            model_name="egsunit",
            name="renewal_started_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="onboardingevent",
            name="secret_ref",
            field=models.CharField(
                blank=True,
                default="",
                max_length=300,
                verbose_name="Secret reference / مرجع السر",
            ),
        ),
        migrations.AlterField(
            model_name="onboardingevent",
            name="action",
            field=models.CharField(
                choices=[
                    ("csr_generated", "CSR generated / أُنشئ طلب الشهادة"),
                    ("otp_entered", "OTP entered / أُدخل رمز التحقق"),
                    ("otp_rejected", "OTP rejected / رُفض رمز التحقق"),
                    (
                        "compliance_csid_issued",
                        "Compliance CSID issued / صدرت شهادة الامتثال",
                    ),
                    (
                        "compliance_checks_passed",
                        "Compliance checks passed / اجتازت فحوص الامتثال",
                    ),
                    (
                        "compliance_checks_failed",
                        "Compliance checks failed / لم تجتز فحوص الامتثال",
                    ),
                    ("pcsid_issued", "Production CSID issued / صدرت شهادة الإنتاج"),
                    ("pcsid_renewed", "Production CSID renewed / جُددت شهادة الإنتاج"),
                    ("revoked", "Revoked / أُلغيت"),
                    ("environment_changed", "Environment changed / تغيرت البيئة"),
                    ("delegation_granted", "Delegation granted / مُنح التفويض"),
                    ("delegation_revoked", "Delegation revoked / أُلغي التفويض"),
                    (
                        "vat_status_changed",
                        "VAT status changed / تغيرت الحالة الضريبية",
                    ),
                    (
                        "secret_store_unavailable",
                        "Secret store unavailable / مخزن الأسرار غير متاح",
                    ),
                    (
                        "secret_store_available",
                        "Secret store available / مخزن الأسرار متاح",
                    ),
                    (
                        "master_key_created",
                        "Master key created / أُنشئ المفتاح الرئيسي",
                    ),
                    (
                        "master_key_rotated",
                        "Master key rotated / دُوِّر المفتاح الرئيسي",
                    ),
                    (
                        "super_key_registered",
                        "Group super key registered / سُجل المفتاح الأعلى",
                    ),
                    ("profile_enabled", "Profile enabled / فُعّل الملف"),
                    (
                        "secret_retirement_requested",
                        "Secret version retirement requested / طُلب إيقاف إصدار السر",
                    ),
                    (
                        "secret_version_retired",
                        "Secret version retired / أُوقف إصدار السر",
                    ),
                    (
                        "secret_retirement_failed",
                        "Secret version retirement failed / تعذر إيقاف إصدار السر",
                    ),
                    (
                        "renewal_needs_attention",
                        "Renewal needs attention / التجديد يحتاج إلى متابعة",
                    ),
                    ("attention_cleared", "Attention cleared / انتهت المتابعة"),
                ],
                max_length=30,
                verbose_name="Action / الإجراء",
            ),
        ),
        migrations.AddConstraint(
            model_name="onboardingevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(("action", "secret_version_retired")),
                fields=("entity", "secret_ref"),
                name="zatca_one_retired_event_per_version",
            ),
        ),
        migrations.RunPython(forward, reverse),
    ]
