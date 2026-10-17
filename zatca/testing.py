"""Test helpers for zatca: a fake ZATCA API with a fake CA, and a local secret store. Never used outside tests.

Nothing here calls a network service; every identifier, key and certificate is made up at run time.
"""
import base64
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

from . import client as zatca_client
from . import crypto, keystore, qr, ubl
from .client import ApiResponse


class FakeCA:
    """Issues certificates for CSRs, signed by a throwaway secp256k1 key (like ZATCA's test sub-CA)."""

    def __init__(self, valid_from=None, valid_days=365 * 3):
        self.key = crypto.generate_private_key()
        self.name = x509.Name([x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "local"),
                               x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "test"),
                               x509.NameAttribute(NameOID.COMMON_NAME, "FAKE-TEST-ZATCA-SubCA")])
        self.valid_from = valid_from
        self.valid_days = valid_days

    def issue(self, csr, valid_from=None, valid_days=None):
        start = valid_from or self.valid_from or datetime.now(timezone.utc) - timedelta(days=1)
        cert = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.name)
                .public_key(csr.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(start + timedelta(days=valid_days or self.valid_days))
                .sign(self.key, hashes.SHA256()))
        return cert


def _validation(errors=(), warnings=()):
    status = "ERROR" if errors else ("WARNING" if warnings else "PASS")
    return {"status": status, "infoMessages": [], "errorMessages": list(errors), "warningMessages": list(warnings)}


def zatca_error(code, message, category="KSA"):
    return {"type": "ERROR", "code": code, "category": category, "message": message, "status": "ERROR"}


def zatca_warning(code, message, category="KSA"):
    return {"type": "WARNING", "code": code, "category": category, "message": message, "status": "WARNING"}


class FakeZatcaClient:
    """Sandbox-shaped fake of the ZATCA APIs. ``script[api]`` holds responses to return first (FIFO, each an
    ``ApiResponse`` or a callable(body) -> ApiResponse); afterwards the default behaviour applies."""

    def __init__(self, ca=None):
        self.ca = ca or FakeCA()
        self.calls = []  # (api, environment, body)
        self.script = {}
        self.rejected_otps = set()
        self.fail_compliance_types = set()  # e.g. {("standard", "383")}
        self.issued_secrets = []
        self.issued_tokens = []
        self._csr_by_request = {}
        self.cert_valid_days = None
        self.cert_valid_from = None

    # -- helpers --
    def _scripted(self, api, body):
        queue = self.script.get(api) or []
        if queue:
            item = queue.pop(0)
            return item(body) if callable(item) else item
        return None

    def _issue(self, csr):
        cert = self.ca.issue(csr, self.cert_valid_from, self.cert_valid_days)
        token = base64.b64encode(crypto.cert_base64(cert).encode()).decode()
        secret = "fake-secret-" + secrets.token_hex(8)
        request_id = str(1_000_000 + len(self.issued_tokens))
        self.issued_secrets.append(secret)
        self.issued_tokens.append(token)
        self._csr_by_request[request_id] = csr
        return ApiResponse(200, {"requestID": request_id, "dispositionMessage": "ISSUED",
                                 "binarySecurityToken": token, "secret": secret})

    @staticmethod
    def _csr(csr_b64):
        return x509.load_pem_x509_csr(base64.b64decode(csr_b64))

    # -- APIs --
    def compliance_csid(self, environment, csr_b64, otp):
        self.calls.append(("compliance_csid", environment, {"csr": csr_b64}))
        scripted = self._scripted("compliance_csid", None)
        if scripted:
            return scripted
        if otp in self.rejected_otps:
            return ApiResponse(400, {"errors": [{"code": "Invalid-OTP", "message": "The provided OTP is invalid"}]})
        return self._issue(self._csr(csr_b64))

    def compliance_check(self, environment, auth, body):
        self.calls.append(("compliance_check", environment, body))
        scripted = self._scripted("compliance_check", body)
        if scripted:
            return scripted
        xml = base64.b64decode(body["invoice"])
        type_code = ubl.find_text(xml, "/*/cbc:InvoiceTypeCode")
        subtype = ubl.find_text(xml, "/*/cbc:InvoiceTypeCode/@name")
        category = "standard" if subtype.startswith("01") else "simplified"
        if (category, type_code) in self.fail_compliance_types:
            return ApiResponse(400, {"validationResults": _validation([zatca_error("BR-KSA-TEST", "Fake failure")])})
        key = "clearanceStatus" if category == "standard" else "reportingStatus"
        return ApiResponse(200, {"validationResults": _validation(), key: "CLEARED" if key[0] == "c" else "REPORTED"})

    def production_csid(self, environment, auth, compliance_request_id):
        self.calls.append(("production_csid", environment, {"compliance_request_id": compliance_request_id}))
        scripted = self._scripted("production_csid", None)
        if scripted:
            return scripted
        return self._issue(self._csr_by_request[compliance_request_id])

    def renew_production_csid(self, environment, auth, csr_b64, otp):
        self.calls.append(("renew_production_csid", environment, {"csr": csr_b64}))
        scripted = self._scripted("renew_production_csid", None)
        if scripted:
            return scripted
        if otp in self.rejected_otps:
            return ApiResponse(400, {"errors": [{"code": "Invalid-OTP", "message": "The provided OTP is invalid"}]})
        return self._issue(self._csr(csr_b64))

    def clearance(self, environment, auth, body, language="en"):
        self.calls.append(("clearance", environment, body))
        scripted = self._scripted("clearance", body)
        if scripted:
            return scripted
        return self.cleared(body)

    def reporting(self, environment, auth, body, language="en"):
        self.calls.append(("reporting", environment, body))
        scripted = self._scripted("reporting", body)
        if scripted:
            return scripted
        return ApiResponse(200, {"validationResults": _validation(), "reportingStatus": "REPORTED"})

    # -- response builders --
    @staticmethod
    def cleared(body, warnings=()):
        """ZATCA's cleared XML: the signed XML with a QR added (tags 1 to 8, no tag 9; R-15)."""
        xml = base64.b64decode(body["invoice"])
        g = lambda xp: ubl.find_text(xml, xp)  # noqa: E731
        cert = crypto.load_cert_base64(g("//ds:X509Certificate"))
        values = [(1, g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyLegalEntity/cbc:RegistrationName").encode()),
                  (2, g("/*/cac:AccountingSupplierParty/cac:Party/cac:PartyTaxScheme/cbc:CompanyID").encode()),
                  (3, f"{g('/*/cbc:IssueDate')}T{g('/*/cbc:IssueTime').rstrip('Z')}Z".encode()),
                  (4, g("/*/cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount").encode()),
                  (5, g("/*/cac:TaxTotal/cbc:TaxAmount").encode()), (6, body["invoiceHash"].encode()),
                  (7, g("//ds:SignatureValue").encode()), (8, crypto.spki_der(cert.public_key()))]
        qr_b64 = qr.encode(values)
        text = xml.decode("utf-8")
        marker = "<cac:Signature>"
        text = text.replace(marker, ubl.QR_ADR.format(qr=qr_b64) + marker, 1)
        status = 202 if warnings else 200
        return ApiResponse(status, {"validationResults": _validation(warnings=warnings),
                                    "clearanceStatus": "CLEARED",
                                    "clearedInvoice": base64.b64encode(text.encode()).decode()})

    @staticmethod
    def rejected(*errors):
        return ApiResponse(400, {"validationResults": _validation(errors), "clearanceStatus": "NOT_CLEARED"})

    @staticmethod
    def accepted_with_warnings(api, *warnings):
        key = "reportingStatus" if api == "reporting" else "clearanceStatus"
        return ApiResponse(202, {"validationResults": _validation(warnings=warnings), key: "REPORTED"})

    @staticmethod
    def timeout():
        return ApiResponse(None, {}, "TimeoutError")


@contextmanager
def fake_zatca_services():
    """Install a fresh local secret store and fake ZATCA client for the duration of a test."""
    store, fake = keystore.LocalSecretStore(), FakeZatcaClient()
    previous_store, previous_client = keystore.override(store), zatca_client.override(fake)
    try:
        yield store, fake
    finally:
        keystore.override(previous_store)
        zatca_client.override(previous_client)
