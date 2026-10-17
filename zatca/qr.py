"""QR code TLV for simplified documents (zatca R-15, R-37; DTG §6 p.58-62). Pure functions.

Pending a current ZATCA SDK or the sandbox (OQ-28 remainder, OQ-33), two points are behind named settings:

- ``ZATCA_QR_TAGS_7_9_LAYOUT``: ``"dtg_2023_pending_sdk_confirm"`` (default, the target: 7 = ECDSA signature
  as base64 text, 8 = public key as DER SubjectPublicKeyInfo, 9 = the certificate's signature, DER) or
  ``"sdk_2_0_3"`` (7 = SubjectPublicKeyInfo, 8 = ECDSA r, 9 = ECDSA s; the old SDK's layout, only for the
  offline SDK comparison, AT-69).
- ``ZATCA_QR_TLV_LENGTH_FORM``: only ``"one_byte_max_127_pending_sdk_confirm"``: every value is at most 127
  bytes, so its length is one byte under both the DTG reading (1-byte length) and the SDK's BER encoding.
"""
import base64

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

LAYOUT_DTG = "dtg_2023_pending_sdk_confirm"
LAYOUT_SDK_203 = "sdk_2_0_3"
LENGTH_ONE_BYTE_127 = "one_byte_max_127_pending_sdk_confirm"
MAX_VALUE_BYTES = 127  # R-15 interim cap
DEFAULT_MAX_CHARS = 700  # R-37 (SDK 2.0.3 still caps at 500; 700 is the target)


class QrValueTooLong(ValueError):
    def __init__(self, tag, length):
        self.tag, self.length = tag, length
        super().__init__(f"QR tag {tag}: {length} bytes > {MAX_VALUE_BYTES}")


class QrTooLong(ValueError):
    def __init__(self, length, limit):
        self.length, self.limit = length, limit
        super().__init__(f"QR base64 {length} chars > {limit}")


def layout():
    value = getattr(settings, "ZATCA_QR_TAGS_7_9_LAYOUT", LAYOUT_DTG)
    if value not in (LAYOUT_DTG, LAYOUT_SDK_203):
        raise ImproperlyConfigured(f"ZATCA_QR_TAGS_7_9_LAYOUT={value!r} is not a known layout")
    return value


def _length_form():
    value = getattr(settings, "ZATCA_QR_TLV_LENGTH_FORM", LENGTH_ONE_BYTE_127)
    if value != LENGTH_ONE_BYTE_127:
        raise ImproperlyConfigured("ZATCA_QR_TLV_LENGTH_FORM supports only the interim 1-byte / 127-byte form")
    return value


def max_chars():
    return int(getattr(settings, "ZATCA_QR_MAX_CHARS", DEFAULT_MAX_CHARS))


def tlv(tag: int, value: bytes) -> bytes:
    _length_form()
    if len(value) > MAX_VALUE_BYTES:
        raise QrValueTooLong(tag, len(value))
    return bytes([tag, len(value)]) + value


def _java_bytes(n: int) -> bytes:
    """``BigInteger.toByteArray()``: minimal signed big-endian (SDK 2.0.3 layout only)."""
    return n.to_bytes(n.bit_length() // 8 + 1, "big")


def encode(values) -> str:
    """``values``: [(tag, bytes), ...] -> base64 text; refuses over-long values and QR (R-15, R-37)."""
    data = b"".join(tlv(tag, value) for tag, value in values)
    text = base64.b64encode(data).decode("ascii")
    if len(text) > max_chars():
        raise QrTooLong(len(text), max_chars())
    return text


def simplified_values(*, seller_name, vat_number, timestamp, total, vat_total, invoice_hash, signature_b64,
                      public_key_der, certificate_signature, signature_r_s=None):
    """Tags 1 to 9 of a simplified document (R-15; DG p.18, 60)."""
    base = [(1, seller_name.encode("utf-8")), (2, vat_number.encode("utf-8")), (3, timestamp.encode("utf-8")),
            (4, total.encode("utf-8")), (5, vat_total.encode("utf-8")), (6, invoice_hash.encode("ascii"))]
    if layout() == LAYOUT_SDK_203:
        r, s = signature_r_s
        return base + [(7, public_key_der), (8, _java_bytes(r)), (9, _java_bytes(s))]
    return base + [(7, signature_b64.encode("ascii")), (8, public_key_der), (9, certificate_signature)]


def decode(text: str) -> dict:
    """base64 TLV -> {tag: bytes}. Reads 1-byte lengths and the BER long form ``0x81 LL`` (as SDK 2.0.3 writes)."""
    data = base64.b64decode(text)
    out, i = {}, 0
    while i < len(data):
        tag, length = data[i], data[i + 1]
        i += 2
        if length == 0x81:
            length = data[i]
            i += 1
        out[tag] = data[i:i + length]
        i += length
    return out
