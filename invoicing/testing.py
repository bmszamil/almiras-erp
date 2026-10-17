"""Test helpers for invoicing: a fake ZATCA port that clears, reports or rejects on demand.

Never used outside tests; nothing here calls an external service.
"""
from contextlib import contextmanager
from itertools import count

from core import access

from . import services, zatca_port
from .zatca_port import ZatcaResult

_icv = count(1)


class FakeZatcaPort:
    """Records submissions like the default port; the test then decides the ZATCA outcome."""

    def __init__(self):
        self.submitted = []

    def submit_document(self, document, user):
        zatca_port.record_submission(document)
        self.submitted.append((document.pk, document.zatca_profile, user.pk))

    def is_issued(self, document):
        return zatca_port.issued_by_status(document)

    def respond(self, document, status, **fields):
        queue = access.system_user("zatca_queue")
        return services.on_zatca_result(document, ZatcaResult(status=status, **fields), user=queue)

    def clear(self, document, status="cleared"):
        return self.respond(document, status, icv=next(_icv), invoice_hash="fake-hash", qr="ZmFrZQ==")

    def report(self, document, status="reported"):
        return self.respond(document, status, icv=next(_icv), invoice_hash="fake-hash", qr="ZmFrZQ==")

    def reject(self, document):
        return self.respond(document, "rejected", messages=(
            {"code": "BR-KSA-TEST", "text_en": "Fake rejection", "text_ar": "رفض تجريبي"},))


@contextmanager
def fake_zatca():
    port = FakeZatcaPort()
    previous, zatca_port._override = zatca_port._override, port
    try:
        yield port
    finally:
        zatca_port._override = previous
