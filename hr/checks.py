"""hr.E001 (R10): personal data needs an encryption key from outside the database when DEBUG is off."""
from django.conf import settings
from django.core.checks import Error, register


@register("hr")
def personal_data_key_configured(app_configs=None, **kwargs):
    if settings.DEBUG or getattr(settings, "HR_PERSONAL_DATA_KEYS", None):
        return []
    return [Error(
        "HR_PERSONAL_DATA_KEYS is empty: HR personal data cannot be encrypted (hr R10).",
        hint="Set the HR_PERSONAL_DATA_KEYS environment variable from the KSA-hosted secret store (H7).",
        id="hr.E001",
    )]
