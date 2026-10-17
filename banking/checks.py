"""banking.E001 (R4): bank identifiers need an encryption key from outside the database when DEBUG is off."""
from django.conf import settings
from django.core.checks import Error, register


@register("banking")
def banking_data_key_configured(app_configs=None, **kwargs):
    if settings.DEBUG or getattr(settings, "BANKING_DATA_KEYS", None):
        return []
    return [Error(
        "BANKING_DATA_KEYS is empty: IBANs and statement data cannot be encrypted (banking R4).",
        hint="Set the BANKING_DATA_KEYS environment variable from the KSA-hosted secret store.",
        id="banking.E001",
    )]
