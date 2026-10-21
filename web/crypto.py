"""Encryption at rest for MFA secrets and keyed hashes for recovery codes (web.md 3.3, R16, OQ-2).

The same design as the project's existing helpers (``hr/crypto.py``, ``banking/crypto.py``): Fernet (AES-128-CBC +
HMAC-SHA256) from the open-source ``cryptography`` package, keys outside the database in
``settings.WEB_MFA_KEYS`` (environment ``WEB_MFA_KEYS``, a Docker secret in deployment; comma-separated, first =
current key, every key decrypts, for rotation). Its own keys, so an MFA secret never shares a key with HR or banking
data.

Recovery codes are random (50 bits) and single use, so they are stored as a keyed HMAC-SHA256, not a slow password
hash: the key is derived from the MFA keys, and a code is checked against its hash under every configured key so a
key rotation does not invalidate the codes already handed out.

Without keys a development machine (``DEBUG``) derives one from ``SECRET_KEY``; with DEBUG off nothing is encrypted
and the deploy check ``web.E002`` refuses the release (web/checks.py).
"""
import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

PREFIX = "wenc1:"


def keys_configured():
    return bool(tuple(getattr(settings, "WEB_MFA_KEYS", None) or ()))


def _secrets():
    keys = tuple(getattr(settings, "WEB_MFA_KEYS", None) or ())
    if keys:
        return keys
    if settings.DEBUG:
        return (f"dev-only:{settings.SECRET_KEY}",)
    raise ImproperlyConfigured("WEB_MFA_KEYS is not set: MFA secrets cannot be encrypted (web.md R16). / لم يُضبط "
                               "WEB_MFA_KEYS: لا يمكن تشفير أسرار المصادقة الثنائية.")


def _derive(secret, purpose):
    return hashlib.sha256(f"almiras-web-mfa:{purpose}:{secret}".encode("utf-8")).digest()


@lru_cache(maxsize=8)
def _fernet(keys):
    return MultiFernet([Fernet(base64.urlsafe_b64encode(_derive(k, "enc"))) for k in keys])


def encrypt(text):
    if text in (None, ""):
        return text
    return PREFIX + _fernet(_secrets()).encrypt(str(text).encode("utf-8")).decode("ascii")


def decrypt(value):
    if value in (None, "") or not str(value).startswith(PREFIX):
        return value
    try:
        return _fernet(_secrets()).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ImproperlyConfigured("An MFA secret cannot be decrypted with the configured keys (web.md R16).") from None


def _hmac(secret, text):
    return hmac.new(_derive(secret, "hash"), str(text).encode("utf-8"), hashlib.sha256).hexdigest()


def code_hash(text):
    """The keyed hash of a recovery code under the current key."""
    return _hmac(_secrets()[0], text)


def code_hashes(text):
    """The code's hash under every configured key, current first (codes issued before a key rotation)."""
    return [_hmac(secret, text) for secret in _secrets()]


class EncryptedSecretField(models.TextField):
    """A secret stored encrypted (Fernet); read back in clear only in memory."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value)
