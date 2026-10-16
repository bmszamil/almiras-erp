"""System check (R3): Django groups and user_permissions give no business rights.

They are a hint that someone tried to grant rights outside memberships, so
non-superusers holding any fail the check. Runs only when Django passes
databases (``manage.py check --database default``, ``migrate``), following
Django's guidance that checks do not touch the database by default.
"""
from django.core.checks import Error, register
from django.db import DatabaseError


@register("access", "database")
def no_django_groups_or_user_permissions(app_configs=None, databases=None, **kwargs):
    if not databases:
        return []
    from django.contrib.auth import get_user_model

    User = get_user_model()
    errors = []
    try:
        for alias in databases:
            qs = User.objects.using(alias).filter(is_superuser=False).filter(
                models_q()
            ).values_list("username", flat=True).distinct()
            for username in qs:
                errors.append(
                    Error(
                        f"User {username!r} has Django groups or user_permissions; business rights come only from memberships.",
                        hint="Remove the groups/permissions and grant a Membership with core.access.grant_membership.",
                        id="access.E001",
                    )
                )
    except DatabaseError:  # tables not migrated yet
        return []
    return errors


def models_q():
    from django.db.models import Q

    return Q(groups__isnull=False) | Q(user_permissions__isnull=False)


@register("access")
def grant_max_age_is_positive(app_configs=None, **kwargs):
    """R31: ACCESS_GRANT_MAX_AGE (seconds or timedelta) must be above zero, else no grant could ever be used
    (or, if negative, the expiry check would be meaningless)."""
    from datetime import timedelta

    from django.conf import settings

    value = getattr(settings, "ACCESS_GRANT_MAX_AGE", None)
    if value is None:
        return []
    if isinstance(value, bool) or not isinstance(value, (int, float, timedelta)):
        seconds = None
    else:
        seconds = value.total_seconds() if isinstance(value, timedelta) else value
    if seconds is None or seconds <= 0:
        return [Error(
            f"ACCESS_GRANT_MAX_AGE must be a positive number of seconds or a positive timedelta, not {value!r}.",
            hint="Set ACCESS_GRANT_MAX_AGE_SECONDS to e.g. 900 (15 minutes, access.md R31).",
            id="access.E002",
        )]
    return []
