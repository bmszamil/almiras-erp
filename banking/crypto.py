"""Encryption at rest for bank identifiers and statement data (banking R4, access R29, PDPL [CC-verify]).

Fernet (AES-128-CBC + HMAC-SHA256) from the open-source ``cryptography`` package. Keys come from
``settings.BANKING_DATA_KEYS`` (environment, never the database); the first key encrypts, every key decrypts
(rotation). IBANs are also stored as a keyed HMAC-SHA256 (``iban_hash``) so uniqueness and lookup work on
ciphertext columns, and statement line hashes (R13) are keyed too, so a hash of a short narrative cannot be
guessed back.

Rotation note: hashes use the first key, so replacing the first key needs a re-hash of ``iban_hash`` and
``line_hash`` (operator task, not built).
"""
import base64
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.validators import MaxLengthValidator
from django.db import models

PREFIX = "benc1:"


def _secrets():
    keys = tuple(getattr(settings, "BANKING_DATA_KEYS", None) or ())
    if not keys:
        if not settings.DEBUG:
            raise ImproperlyConfigured("BANKING_DATA_KEYS is not set (banking R4).")
        keys = (f"dev-only:{settings.SECRET_KEY}",)
    return keys


def _derive(secret, purpose):
    return hashlib.sha256(f"almiras-banking:{purpose}:{secret}".encode("utf-8")).digest()


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
        raise ImproperlyConfigured("Banking data cannot be decrypted with the configured keys (banking R4).") from None


def keyed_hash(text):
    """HMAC-SHA256 hex of ``text`` with a key derived from the current secret."""
    return hmac.new(_derive(_secrets()[0], "hash"), str(text).encode("utf-8"), hashlib.sha256).hexdigest()


class EncryptedTextField(models.TextField):
    """Text stored encrypted; ``plain_max_length`` limits the plain text."""

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
