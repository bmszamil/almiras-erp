"""ZATCA cryptography with lxml + cryptography only (zatca R-13, R-14, §3.2). Pure functions, no database.

The approach is the one proven against ZATCA SDK 2.0.3 in ``docs/compliance/zatca-sdk-kit/scripts``
(``pyhash.py``, ``pysign.py``; findings §1 to §6):

- invoice hash: drop ``ext:UBLExtensions``, the ``cac:Signature`` child of the root and the QR
  ``cac:AdditionalDocumentReference``, canonicalise with lxml ``method="c14n"`` (C14N 1.0, byte-identical
  to the SDK's C14N 1.1 for whole UBL documents; never ``c14n2``), SHA-256, base64 of the raw digest;
- signature: ``SHA256withECDSA`` on curve secp256k1 over the **raw 32-byte invoice hash**; SignedInfo is
  not canonicalised or signed; DER (r, s);
- CertDigest: base64(hex(SHA-256(base64 certificate text))); SignedProperties digest: base64(hex(SHA-256(the
  element's bytes as written))).
"""
import base64
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier
from lxml import etree

NS = {
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
    "xades": "http://uri.etsi.org/01903/v1.3.2#",
}

# R-12 / BR-KSA-26: base64 of the lowercase hex text of SHA-256("0"). A literal, never computed (AT-10).
INITIAL_PIH = "NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=="

CURVE = ec.SECP256K1()  # OID 1.3.132.0.10 (R-14)
CURVE_OID = "1.3.132.0.10"
TEMPLATE_NAME_OID = ObjectIdentifier("1.3.6.1.4.1.311.20.2")  # Microsoft certificate template name (§3.2)
OID_SN = NameOID.SURNAME  # 2.5.4.4: the EGS serial in the SAN DirectoryName (SDK 2.0.3, findings §6)
OID_UID = NameOID.USER_ID  # 0.9.2342.19200300.100.1.1: VAT number
OID_TITLE = NameOID.TITLE  # 2.5.4.12: invoice-type flag, e.g. 1100
OID_REGISTERED_ADDRESS = ObjectIdentifier("2.5.4.26")
OID_BUSINESS_CATEGORY = NameOID.BUSINESS_CATEGORY  # 2.5.4.15


def _parser():
    # Comments and character references kept as the SDK sees them; no entity expansion, no network (XXE).
    return etree.XMLParser(remove_comments=False, resolve_entities=False, no_network=True)


def canonical_for_hash(xml: bytes) -> bytes:
    """R-13 steps (1) to (3): the canonical bytes the invoice hash is taken over."""
    root = etree.fromstring(xml, _parser())
    for xp in ("//ext:UBLExtensions", "/*/cac:Signature", "//cac:AdditionalDocumentReference[cbc:ID='QR']"):
        for el in root.xpath(xp, namespaces=NS):
            parent, prev, tail = el.getparent(), el.getprevious(), el.tail
            if tail:  # keep the whitespace that followed the removed element (as the SDK's XSLT does)
                if prev is not None:
                    prev.tail = (prev.tail or "") + tail
                else:
                    parent.text = (parent.text or "") + tail
            parent.remove(el)
    return etree.tostring(root.getroottree(), method="c14n", with_comments=False)


def invoice_hash_raw(xml: bytes) -> bytes:
    return hashlib.sha256(canonical_for_hash(xml)).digest()


def invoice_hash(xml: bytes) -> str:
    """R-13: base64 of the raw 32-byte SHA-256 digest (44 characters)."""
    return base64.b64encode(invoice_hash_raw(xml)).decode("ascii")


# --- keys and signatures --------------------------------------------------------------------------

def generate_private_key():
    return ec.generate_private_key(CURVE)


def private_key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def load_private_key(pem: bytes):
    return serialization.load_pem_private_key(pem, None)


def sign_hash(private_key, raw_hash: bytes) -> bytes:
    """R-14: ``SHA256withECDSA`` over the raw 32-byte invoice hash; returns the DER signature."""
    if len(raw_hash) != 32:
        raise ValueError("the invoice hash to sign is the raw 32-byte digest")
    return private_key.sign(raw_hash, ec.ECDSA(hashes.SHA256()))


def verify_hash_signature(public_key, signature_der: bytes, raw_hash: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature

    try:
        public_key.verify(signature_der, raw_hash, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False
    return True


def spki_der(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


# --- CSR (§3.2, AT-68) ----------------------------------------------------------------------------

def _utf8_der(text: str) -> bytes:
    data = text.encode("utf-8")
    if len(data) >= 128:
        raise ValueError("template name too long")
    return bytes([0x0C, len(data)]) + data


TEMPLATE_NAMES = {"production": "ZATCA-Code-Signing", "simulation": "PREZATCA-Code-Signing"}


def template_name(environment: str) -> str:
    """§3.2: ``ZATCA-Code-Signing`` in production, ``PREZATCA-Code-Signing`` in simulation (SDK R3.4.8 ``-sim``;
    findings §3.6, OQ-26), ``TSTZATCA-Code-Signing`` otherwise (sandbox; SDK ``-nonprod``)."""
    return TEMPLATE_NAMES.get(environment, "TSTZATCA-Code-Signing")


def build_csr(private_key, *, common_name, org_unit, org_name, serial_number, vat_number, invoice_type, location,
              industry, environment):
    """CSR per the SDK 2.0.3 ``-csr`` layout (findings §6): subject C, OU, O, CN; one SAN DirectoryName with
    SN, UID, title, registeredAddress, businessCategory; template-name extension; ecdsa-with-SHA256."""
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "SA"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, org_unit),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org_name),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    dirname = x509.Name([
        x509.NameAttribute(OID_SN, serial_number),
        x509.NameAttribute(OID_UID, vat_number),
        x509.NameAttribute(OID_TITLE, invoice_type),
        x509.NameAttribute(OID_REGISTERED_ADDRESS, location),
        x509.NameAttribute(OID_BUSINESS_CATEGORY, industry),
    ])
    builder = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(subject)
        .add_extension(x509.UnrecognizedExtension(TEMPLATE_NAME_OID, _utf8_der(template_name(environment))),
                       critical=False)
        .add_extension(x509.SubjectAlternativeName([x509.DirectoryName(dirname)]), critical=False)
    )
    return builder.sign(private_key, hashes.SHA256())


def csr_pem(csr) -> bytes:
    return csr.public_bytes(serialization.Encoding.PEM)


def csr_base64(csr) -> str:
    """What the compliance / production CSID APIs take: base64 of the PEM text."""
    return base64.b64encode(csr_pem(csr)).decode("ascii")


# --- certificates ---------------------------------------------------------------------------------

def cert_base64_from_token(binary_security_token: str) -> str:
    """ZATCA's ``binarySecurityToken`` is base64 of the base64 DER certificate text; returns the inner text."""
    return base64.b64decode(binary_security_token).decode("ascii").strip()


def load_cert_base64(cert_b64: str):
    return x509.load_der_x509_certificate(base64.b64decode(cert_b64))


def cert_pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def cert_base64(cert) -> str:
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")


def cert_from_pem(pem: str):
    return x509.load_pem_x509_certificate(pem.encode("ascii"))


def hex_digest_b64(data: bytes) -> str:
    """base64 of the lowercase **hex text** of SHA-256(data) (CertDigest, SignedProperties digest)."""
    return base64.b64encode(hashlib.sha256(data).hexdigest().encode("ascii")).decode("ascii")


def cert_digest(cert_b64: str) -> str:
    """R-14 / AT-65: base64(hex(SHA-256(the base64 certificate text as ASCII))); not the DER bytes."""
    return hex_digest_b64(cert_b64.encode("ascii"))


def issuer_name(cert, style="rfc2253") -> str:
    """X509IssuerName (R-14): RFC 2253 order with ", " separators (the SUN form; settled by SDK R3.4.8, whose
    samples all carry it and whose validator accepts it with SUN as the first JCA provider; findings §3.3).

    ``style="bc"`` gives the BouncyCastle form (``DC=local,DC=gov,...``), an artefact of running the SDK with
    BouncyCastle as provider 1; kept only for comparing against such a JVM, never for submission.
    """
    if style == "bc":
        return ",".join(rdn.rfc4514_string() for rdn in cert.issuer.rdns)
    return ", ".join(rdn.rfc4514_string() for rdn in reversed(cert.issuer.rdns))
