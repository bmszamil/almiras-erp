"""Personal-data encryption at rest (hr R10, PDPL [CC-verify]).

Fernet (AES-128-CBC + HMAC-SHA256) from the open-source ``cryptography`` package; the library choice is open
(OQ-11, go-live). Keys come from ``settings.HR_PERSONAL_DATA_KEYS`` (environment, never the database). The
first key encrypts; every key decrypts (rotation).

Values that must be unique (the ID number, R9) are also stored as a keyed HMAC so a partial unique constraint can
work on ciphertext columns. The HMAC key is ``settings.HR_PERSONAL_DATA_HASH_KEY``, a separate, stable secret
that does not change when the encryption keys rotate (review finding 8). Without it the HMAC falls back to the
first encryption key; :func:`keyed_hashes` then also yields the hash under every older key, so a duplicate ID is
still found across a rotation, and ``manage.py hr_rehash_personal_data`` re-hashes (and re-encrypts) every row
under the current keys.

There is no silent development key: a key derived from ``SECRET_KEY`` is used only when
``settings.HR_ALLOW_DEV_KEY`` is True (explicit opt-in, refused with DEBUG off by check hr.E002).
"""
import base64
import hashlib
import hmac
from datetime import date
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.validators import MaxLengthValidator
from django.db import models

PREFIX = "enc1:"


def dev_key_allowed():
    return bool(getattr(settings, "HR_ALLOW_DEV_KEY", False))


def _secrets():
    keys = tuple(getattr(settings, "HR_PERSONAL_DATA_KEYS", None) or ())
    if keys:
        return keys
    if dev_key_allowed():
        return (f"dev-only:{settings.SECRET_KEY}",)
    raise ImproperlyConfigured("HR_PERSONAL_DATA_KEYS is not set: personal data cannot be encrypted (hr R10); set it "
                               "from the secret store, or HR_ALLOW_DEV_KEY=1 on a development machine. / لم يُضبط "
                               "HR_PERSONAL_DATA_KEYS: لا يمكن تشفير البيانات الشخصية؛ اضبطه من مخزن الأسرار، أو "
                               "HR_ALLOW_DEV_KEY=1 على جهاز تطوير.")


def _hash_secrets():
    """The HMAC secrets, current first: the stable hash key if set, then every encryption key (older hashes)."""
    hash_key = getattr(settings, "HR_PERSONAL_DATA_HASH_KEY", "") or ""
    keys = ([hash_key] if hash_key else []) + list(_secrets())
    return tuple(dict.fromkeys(keys))


def _derive(secret, purpose):
    return hashlib.sha256(f"almiras-hr:{purpose}:{secret}".encode("utf-8")).digest()


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
        raise ImproperlyConfigured("Personal data cannot be decrypted with the configured keys (hr R10).") from None


def _hmac(secret, text):
    norm = "".join(str(text or "").split()).upper()
    return hmac.new(_derive(secret, "hash"), norm.encode("utf-8"), hashlib.sha256).hexdigest()


def keyed_hash(text):
    """HMAC-SHA256 of a normalised value under the current hash secret (R9 uniqueness)."""
    return _hmac(_hash_secrets()[0], text)


def keyed_hashes(text):
    """The value's HMAC under every configured secret, current first: what a row hashed before a rotation (or
    before the hash key was set) may still hold."""
    return [_hmac(secret, text) for secret in _hash_secrets()]


def reencrypt(value):
    """``value`` (plain text, or ciphertext under any configured key) encrypted under the current first key."""
    return encrypt(decrypt(value))


class EncryptedTextField(models.TextField):
    """Text stored encrypted; ``max_length`` limits the plain text."""

    def __init__(self, *args, plain_max_length=None, **kwargs):
        self.plain_max_length = plain_max_length
        super().__init__(*args, **kwargs)
        if plain_max_length:
            self.validators.append(MaxLengthValidator(plain_max_length))

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        if self.plain_max_length:
            kwargs["plain_max_length"] = self.plain_max_length
        return name, path, args, kwargs

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value)


class EncryptedDateField(EncryptedTextField):
    """A date stored encrypted as ISO text."""

    def from_db_value(self, value, expression, connection):
        value = decrypt(value)
        return date.fromisoformat(value) if value else None

    def to_python(self, value):
        if isinstance(value, str) and value:
            return date.fromisoformat(value)
        return value

    def get_prep_value(self, value):
        if isinstance(value, date):
            value = value.isoformat()
        return encrypt(value) if value else value
