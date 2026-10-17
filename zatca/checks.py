"""zatca system checks (R-6, R-29): the in-memory secret store never runs outside local development.

``zatca.E001``: ``ZATCA_SECRET_STORE`` is :class:`zatca.keystore.LocalSecretStore` (keys only in process memory,
lost on restart) while ``DEBUG`` is off or ``ZATCA_PRODUCTION_ALLOWED`` is on. Tests install the local store with
``zatca.keystore.override`` and never through this setting.
"""
from django.core.checks import Error, register

from . import keystore


@register("zatca")
def check_secret_store(app_configs=None, **kwargs):
    refusal = keystore.local_store_refusal()
    if not refusal:
        return []
    return [Error(refusal, hint="Point ZATCA_SECRET_STORE at the OpenBao store (zatca.keystore.OpenBaoSecretStore); "
                                "the local store is for tests and local development with DEBUG on only.",
                  id="zatca.E001")]
