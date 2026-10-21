"""Deploy checks of the web UI (``manage.py check --deploy``, run by ``release-check`` in deploy/bin/entrypoint.sh).

* ``web.E001``: MFA (web.md R13 to R16, R19) is switched off (``WEB_MFA_ENFORCED`` / ``DJANGO_WEB_MFA_ENFORCED=0``, or
  the admin gate ``WEB_ADMIN_REQUIRE_MFA``) while
  DEBUG is off: approvers, the Managing Partner, the chairman, CEOs, ZATCA and access admins and superusers would
  sign in with a password only. Since slice 2 MFA is built and on by default, so the check passes unless someone
  switches MFA off.
* ``web.E003``: ``SECRET_KEY`` is the development default or shorter than 50 characters while DEBUG is off.
* ``web.E002``: ``WEB_MFA_KEYS`` missing, shorter than 32 characters, with white space or a placeholder, while DEBUG
  is off: TOTP secrets could not be encrypted at rest (R16, OQ-2).

All are deploy-only checks, so ``migrate``, scheduled jobs and the test suite are not affected.
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


DEV_SECRET_KEY = "dev-only-change-me"  # config/settings.py default


@register(Tags.security, deploy=True)
def secret_key_strong(app_configs=None, **kwargs):
    """web.E003 (verification review of f95e1c8, item 9): with DEBUG off, ``SECRET_KEY`` must not be the development
    default and must be at least 50 characters. It signs sessions and keys the sign-in throttle (R17, R18)."""
    key = settings.SECRET_KEY or ""
    if settings.DEBUG:
        return []
    problems = []
    if key == DEV_SECRET_KEY or key.startswith(("dev-only", "django-insecure")):
        problems.append("it is the development default")
    if len(key) < 50:
        problems.append("it is shorter than 50 characters")
    if not problems:
        return []
    return [Error(
        "SECRET_KEY is not fit for production: " + "; ".join(problems) + ". / مفتاح SECRET_KEY غير صالح للتشغيل.",
        hint="Set DJANGO_SECRET_KEY (DJANGO_SECRET_KEY_FILE, Docker secret django_secret_key, made by "
             "deploy/init-secrets.sh) to a random value of 50 characters or more.",
        id="web.E003",
    )]


@register(Tags.security, deploy=True)
def mfa_keys_configured(app_configs=None, **kwargs):
    from .crypto import key_problems

    if settings.DEBUG:
        return []
    problems = key_problems()
    if not problems:
        return []
    return [Error(
        "WEB_MFA_KEYS is not usable: TOTP secrets cannot be encrypted at rest (web.md R16): " + "; ".join(problems)
        + ". / مفاتيح WEB_MFA_KEYS غير صالحة.",
        hint="Set WEB_MFA_KEYS (WEB_MFA_KEYS_FILE, Docker secret web_mfa_keys, made by deploy/init-secrets.sh): "
             "comma-separated random keys of 32 characters or more.",
        id="web.E002",
    )]
