"""QR code TLV for simplified documents (zatca R-15, R-37; DTG §6 p.58-62). Pure functions.

Settled offline against ZATCA SDK R3.4.8 (``docs/compliance/zatca-sdk-3-findings-2026-10-17.md`` §3.1):

- Lengths are BER (the SDK's ``BerTlvBuilder``): one byte up to 127, ``0x81 LL`` for 128-255, ``0x82 LLLL`` above.
  A 1-byte length for a 128-255-byte value fails the SDK's QR check (``QRCODE_INVALID``). No per-value cap.
- Tags 7-9 follow the DTG, which is the SDK's layout: 7 = ``ds:SignatureValue`` as base64 text, 8 = public key as
  DER SubjectPublicKeyInfo, 9 = the certificate's own signature (DER); tag 9 only on simplified documents. The
  SDK 2.0.3 layout (7 SPKI, 8 r, 9 s) fails R3.4.8 and is retired.
- The QR is at most 1000 base64 characters (BR-CL-KSA-14 in R3.4.8; R-37).
- The R3.4.8 validator does not fail a QR with missing tags, so presence is checked here (``missing_tags``) and
  by AT-14, not left to the SDK.
"""
import base64

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

LAYOUT_DTG = "dtg"
MAX_CHARS = 1000  # R-37 / BR-CL-KSA-14 (SDK R3.4.8)
SIMPLIFIED_TAGS = tuple(range(1, 10))  # R-15: simplified documents and their notes carry tags 1 to 9
STANDARD_TAGS = tuple(range(1, 9))  # ZATCA's QR in a cleared standard document: no tag 9
# Values the retired ZATCA_QR_TAGS_7_9_LAYOUT setting may still hold without changing anything (both mean DTG).
_DTG_SETTING_VALUES = (None, LAYOUT_DTG, "dtg_2023_pending_sdk_confirm")


class QrTooLong(ValueError):
    def __init__(self, length, limit):
        self.length, self.limit = length, limit
        super().__init__(f"QR base64 {length} chars > {limit}")


class QrInvalid(ValueError):
    """A QR text that is not well-formed base64 BER-TLV."""


def layout():
    """Always the DTG layout. The retired SDK 2.0.3 layout is refused loudly rather than silently ignored."""
    value = getattr(settings, "ZATCA_QR_TAGS_7_9_LAYOUT", None)
    if value not in _DTG_SETTING_VALUES:
        raise ImproperlyConfigured(f"ZATCA_QR_TAGS_7_9_LAYOUT={value!r}: only the DTG layout exists (SDK R3.4.8 "
                                   "findings §3.1); remove the setting")
    return LAYOUT_DTG


def max_chars():
    return MAX_CHARS


def _ber_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, value: bytes) -> bytes:
    """One TLV: 1-byte tag, BER length, value."""
    if not 1 <= tag <= 0xFF:
        raise ValueError(f"QR tag {tag} is not one byte")
    return bytes([tag]) + _ber_length(len(value)) + value


def encode(values) -> str:
    """``values``: [(tag, bytes), ...] -> base64 text; refuses a QR over 1000 characters (R-37)."""
    data = b"".join(tlv(tag, value) for tag, value in values)
    text = base64.b64encode(data).decode("ascii")
    if len(text) > MAX_CHARS:
        raise QrTooLong(len(text), MAX_CHARS)
    return text


def simplified_values(*, seller_name, vat_number, timestamp, total, vat_total, invoice_hash, signature_b64,
                      public_key_der, certificate_signature):
    """Tags 1 to 9 of a simplified document (R-15; DG p.18, 60). ``total`` is BT-115 PayableAmount (tag 4)."""
    layout()
    return [(1, seller_name.encode("utf-8")), (2, vat_number.encode("utf-8")), (3, timestamp.encode("utf-8")),
            (4, total.encode("utf-8")), (5, vat_total.encode("utf-8")), (6, invoice_hash.encode("ascii")),
            (7, signature_b64.encode("ascii")), (8, public_key_der), (9, certificate_signature)]


def decode(text: str) -> dict:
    """base64 BER-TLV -> {tag: bytes}. Raises :class:`QrInvalid` on bad base64, a truncated value, an
    indefinite or over-long length, or a repeated tag."""
    try:
        data = base64.b64decode(text, validate=True)
    except (ValueError, TypeError) as exc:
        raise QrInvalid(f"QR is not base64: {exc}") from None
    out, i = {}, 0
    while i < len(data):
        if i + 2 > len(data):
            raise QrInvalid("QR ends inside a tag header")
        tag, length = data[i], data[i + 1]
        i += 2
        if length & 0x80:
            count = length & 0x7F
            if count == 0 or count > 2 or i + count > len(data):
                raise QrInvalid(f"QR tag {tag}: unsupported BER length form 0x{length:02x}")
            length = int.from_bytes(data[i:i + count], "big")
            i += count
        if i + length > len(data):
            raise QrInvalid(f"QR tag {tag}: value runs past the end")
        if tag in out:
            raise QrInvalid(f"QR tag {tag} appears twice")
        out[tag] = data[i:i + length]
        i += length
    return out


def missing_tags(decoded: dict, simplified: bool) -> list:
    """Tags required by R-15 that are absent or empty: 1-9 for simplified, 1-8 for standard."""
    return [t for t in (SIMPLIFIED_TAGS if simplified else STANDARD_TAGS) if not decoded.get(t)]
