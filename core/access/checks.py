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
