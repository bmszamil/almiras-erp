"""Encryption at rest for MFA secrets and keyed hashes for recovery codes (web.md 3.3, R16, OQ-2).

The same design as the project's existing helpers (``hr/crypto.py``, ``banking/crypto.py``): Fernet (AES-128-CBC +
HMAC-SHA256) from the open-source ``cryptography`` package, keys outside the database in
``settings.WEB_MFA_KEYS`` (environment ``WEB_MFA_KEYS``, a Docker secret in deployment; comma-separated, first =
current key, every key decrypts, for rotation). Its own keys, so an MFA secret never shares a key with HR or banking
data.

Recovery codes are random (50 bits) and single use, so they are stored as a keyed HMAC-SHA256, not a slow password
hash: the key is derived from the MFA keys, and a code is checked against its hash under every configured key so a
key rotation does not invalidate the codes already handed out. Each code row records the fingerprint of the key that
hashed it (``key_id``). A one-way hash cannot be re-hashed under a new key without the code, so after a rotation
``manage.py web_mfa_rekey`` re-encrypts every secret and lists (or, with ``--expire-old-recovery-codes``, retires)
the codes still hashed under an older key; their holders renew their codes before the old key is removed.

Stored values without the encryption prefix are refused outside DEBUG (fail closed).

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


MIN_KEY_LENGTH = 32


def key_problems():
    """What is wrong with the configured ``WEB_MFA_KEYS`` (deploy check web.E002): none set, or a key shorter than
    32 characters, with white space, or a development placeholder."""
    keys = tuple(getattr(settings, "WEB_MFA_KEYS", None) or ())
    if not keys:
        return ["missing"]
    problems = []
    for index, key in enumerate(keys, start=1):
        if len(key) < MIN_KEY_LENGTH:
            problems.append(f"key {index}: shorter than {MIN_KEY_LENGTH} characters")
        if any(ch.isspace() for ch in key):
            problems.append(f"key {index}: contains white space")
        if key.startswith("dev-only") or "change-me" in key:
            problems.append(f"key {index}: a development placeholder")
    return problems


def key_id(secret=None):
    """A short public fingerprint of a key (which key hashed a recovery code); never the key itself."""
    secret = secret if secret is not None else _secrets()[0]
    return hashlib.sha256(_derive(secret, "id")).hexdigest()[:16]


def decrypt(value):
    if value in (None, ""):
        return value
    if not str(value).startswith(PREFIX):
        # Fail closed (security review 2026-10-21, item 4): outside DEBUG a stored secret is always encrypted, so
        # a plain value is tampering or a broken migration, never something to use.
        if not settings.DEBUG:
            raise ImproperlyConfigured("An MFA secret is stored unencrypted (web.md R16); refused.")
        return value
    try:
        return _fernet(_secrets()).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise ImproperlyConfigured("An MFA secret cannot be decrypted with the configured keys (web.md R16).") from None


def _hmac(secret, text):
    return hmac.new(_derive(secret, "hash"), str(text).encode("utf-8"), hashlib.sha256).hexdigest()


def code_hash(text):
    """The keyed hash of a recovery code under the current key (stored with :func:`key_id`)."""
    return _hmac(_secrets()[0], text)


def is_current(value):
    """True if ``value`` (stored ciphertext) decrypts with the current first key alone (``web_mfa_rekey``)."""
    if not value or not str(value).startswith(PREFIX):
        return False
    try:
        Fernet(base64.urlsafe_b64encode(_derive(_secrets()[0], "enc"))).decrypt(value[len(PREFIX):].encode("ascii"))
    except InvalidToken:
        return False
    return True


def reencrypt(value):
    """``value`` (ciphertext under any configured key) encrypted under the current first key."""
    return encrypt(decrypt(value))


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
