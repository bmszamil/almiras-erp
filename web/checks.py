"""Deploy checks of the web UI (``manage.py check --deploy``, run by ``release-check`` in deploy/bin/entrypoint.sh).

* ``web.E001``: MFA (web.md R13 to R16, R19) is switched off (``WEB_MFA_ENFORCED`` / ``DJANGO_WEB_MFA_ENFORCED=0``, or
  the admin gate ``WEB_ADMIN_REQUIRE_MFA``) while
  DEBUG is off: approvers, the Managing Partner, the chairman, CEOs, ZATCA and access admins and superusers would
  sign in with a password only. Since slice 2 MFA is built and on by default, so the check passes unless someone
  switches MFA off.
* ``web.E002``: no ``WEB_MFA_KEYS`` while DEBUG is off: TOTP secrets could not be encrypted at rest (R16, OQ-2).

Both are deploy-only checks, so ``migrate``, scheduled jobs and the test suite are not affected.
"""
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def mfa_enforced(app_configs=None, **kwargs):
    if settings.DEBUG or (getattr(settings, "WEB_MFA_ENFORCED", True) and getattr(settings, "WEB_ADMIN_REQUIRE_MFA", True)):
        return []
    return [Error(
        "MFA is switched off: holders of MFA roles and superusers would sign in to the web UI with a password only "
        "(web.md R13 to R16). / المصادقة متعددة العوامل معطلة في واجهة الويب.",
        hint="Go-live blocker. Remove DJANGO_WEB_MFA_ENFORCED=0 (MFA is on by default).",
        id="web.E001",
    )]


@register(Tags.security, deploy=True)
def mfa_keys_configured(app_configs=None, **kwargs):
    if settings.DEBUG or getattr(settings, "WEB_MFA_KEYS", None):
        return []
    return [Error(
        "WEB_MFA_KEYS is not set: TOTP secrets cannot be encrypted at rest (web.md R16). / لم يُضبط WEB_MFA_KEYS.",
        hint="Set WEB_MFA_KEYS (WEB_MFA_KEYS_FILE, Docker secret web_mfa_keys, made by deploy/init-secrets.sh).",
        id="web.E002",
    )]
