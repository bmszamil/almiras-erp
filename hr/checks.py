"""HR system checks (R10, finding 8): personal data needs an encryption key from outside the database; the
development key derived from SECRET_KEY is an explicit opt-in and never allowed with DEBUG off. Messages are
English and Arabic (nit 22)."""
from django.conf import settings
from django.core.checks import Error, Warning, register


@register("hr")
def personal_data_key_configured(app_configs=None, **kwargs):
    keys = getattr(settings, "HR_PERSONAL_DATA_KEYS", None)
    dev = bool(getattr(settings, "HR_ALLOW_DEV_KEY", False))
    hint = ("Set HR_PERSONAL_DATA_KEYS (and HR_PERSONAL_DATA_HASH_KEY) from the KSA-hosted secret store (H7). / "
            "اضبط HR_PERSONAL_DATA_KEYS (وHR_PERSONAL_DATA_HASH_KEY) من مخزن الأسرار المستضاف في المملكة.")
    problems = []
    if dev and not settings.DEBUG:
        problems.append(Error(
            "HR_ALLOW_DEV_KEY is on with DEBUG off: personal data would be encrypted with a key derived from "
            "SECRET_KEY (hr R10). / HR_ALLOW_DEV_KEY مفعّل مع إيقاف DEBUG: ستُشفَّر البيانات الشخصية بمفتاح مشتق من "
            "SECRET_KEY.", hint=hint, id="hr.E002"))
    if not keys and not dev:
        text = ("HR_PERSONAL_DATA_KEYS is empty: HR personal data cannot be encrypted (hr R10). / HR_PERSONAL_DATA_KEYS "
                "فارغ: لا يمكن تشفير البيانات الشخصية للموارد البشرية.")
        problems.append(Warning(text, hint=hint, id="hr.W001") if settings.DEBUG else Error(text, hint=hint, id="hr.E001"))
    if keys and not getattr(settings, "HR_PERSONAL_DATA_HASH_KEY", "") and not settings.DEBUG:
        problems.append(Warning(
            "HR_PERSONAL_DATA_HASH_KEY is not set: the ID-number hash follows the first encryption key; after a key "
            "rotation run manage.py hr_rehash_personal_data (hr R9). / لم يُضبط HR_PERSONAL_DATA_HASH_KEY: يتبع "
            "تجزئة رقم الهوية مفتاح التشفير الأول؛ بعد تدوير المفاتيح شغّل الأمر hr_rehash_personal_data.",
            hint=hint, id="hr.W002"))
    return problems
