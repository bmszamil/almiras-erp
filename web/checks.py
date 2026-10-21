"""Deploy check web.E001: the web UI has no MFA yet (web.md R13 to R16), so a production deployment must not
silently let approvers, the Managing Partner, ZATCA and access admins and superusers sign in with a password only.

Registered as a deploy check (``manage.py check --deploy``, run by ``release-check`` in deploy/bin/entrypoint.sh),
so ``migrate``, scheduled jobs and the test suite are not affected. While DEBUG is off it is an Error unless
``WEB_ALLOW_PASSWORD_ONLY_LOGIN`` (``DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN=1``) is set explicitly: a go-live blocker.
"""
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def password_only_login(app_configs=None, **kwargs):
    if settings.DEBUG or getattr(settings, "WEB_ALLOW_PASSWORD_ONLY_LOGIN", False):
        return []
    return [Error(
        "The web UI has no MFA yet: holders of MFA roles and superusers would sign in with a password only "
        "(web.md R13 to R16). / لا توجد مصادقة متعددة العوامل بعد في واجهة الويب.",
        hint="Go-live blocker. Ship the MFA slice, or set DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN=1 explicitly "
             "(test and CI stacks only).",
        id="web.E001",
    )]
