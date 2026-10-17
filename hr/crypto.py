"""Personal-data encryption at rest (hr R10, PDPL [CC-verify]).

Fernet (AES-128-CBC + HMAC-SHA256) from the open-source ``cryptography`` package; the library choice is open
(OQ-11, go-live). Keys come from ``settings.HR_PERSONAL_DATA_KEYS`` (environment, never the database). The
first key encrypts; every key decrypts (rotation). Values that must be unique (the ID number, R9) are also
stored as a keyed HMAC so a partial unique constraint can work on ciphertext columns.

Note for rotation: the HMAC uses the first key, so replacing the first key needs a re-hash of
``Employee.id_number_hash`` (operator task, not built).
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


def _secrets():
    keys = tuple(getattr(settings, "HR_PERSONAL_DATA_KEYS", None) or ())
    if not keys:
        if not settings.DEBUG:
            raise ImproperlyConfigured("HR_PERSONAL_DATA_KEYS is not set (hr R10).")
        keys = (f"dev-only:{settings.SECRET_KEY}",)
    return keys


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


def keyed_hash(text):
    """HMAC-SHA256 of a normalised value with a key derived from the current secret (R9 uniqueness)."""
    norm = "".join(str(text or "").split()).upper()
    return hmac.new(_derive(_secrets()[0], "hash"), norm.encode("utf-8"), hashlib.sha256).hexdigest()


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
