"""Give recovery codes made before ``key_id`` existed (web/0004) the fingerprint of the key that hashed them, when that
is certain: exactly one MFA key is configured (verification review of f95e1c8, item 4). With several keys the key is
not known; those codes keep a blank ``key_id``, ``web_mfa_rekey`` reports them apart and never retires them.

The fingerprint is computed here as in ``web.crypto.key_id`` (copied, so the migration does not depend on later code).
"""
import hashlib

from django.conf import settings
from django.db import migrations


def _single_key():
    keys = tuple(getattr(settings, "WEB_MFA_KEYS", None) or ())
    if not keys and settings.DEBUG:
        keys = (f"dev-only:{settings.SECRET_KEY}",)
    return keys[0] if len(keys) == 1 else None


def backfill(apps, schema_editor):
    key = _single_key()
    if key is None:
        return
    derived = hashlib.sha256(f"almiras-web-mfa:id:{key}".encode("utf-8")).digest()
    fingerprint = hashlib.sha256(derived).hexdigest()[:16]
    apps.get_model("web", "RecoveryCode").objects.filter(key_id="").update(key_id=fingerprint)


class Migration(migrations.Migration):

    dependencies = [
        ("web", "0004_mfa_lock_escalation_enrolment_expiry_key_id"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
