"""invoicing's ZATCA port implemented by the zatca app (invoicing/zatca_port.py; invoicing R40 to R46).

Selected with ``INVOICING_ZATCA_PORT = "zatca.port.ZatcaModulePort"``. Results reach invoicing through
``invoicing.services.on_zatca_result`` (called by ``zatca.services`` as the ZATCA queue system user).
"""
from . import services


class ZatcaModulePort:
    def submit_document(self, document, user):
        return services.submit_document(document, user)

    def is_issued(self, document):
        return services.is_issued(document)
