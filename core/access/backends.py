"""Authentication backends (access.md appendix, R3, R20, R21)."""
from django.contrib.auth.backends import BaseBackend, ModelBackend

from core.models import Entity

from .models import Kind


class HumanModelBackend(ModelBackend):
    """Django's username/password login, for human users only.

    System users (unusable password) and agent users (gateway tokens only)
    can never sign in through a view, the admin or a session (R20, R21).
    """

    def user_can_authenticate(self, user):
        if not super().user_can_authenticate(user):
            return False
        profile = getattr(user, "access_profile", None)
        return profile is None or profile.kind == Kind.HUMAN

    def get_user(self, user_id):
        user = super().get_user(user_id)
        if user is not None and not self.user_can_authenticate(user):
            return None
        return user


class EntityRoleBackend(BaseBackend):
    """``user.has_perm("app.codename", entity)`` answered by the access layer.

    Returns False without an Entity. Note: Django's ``User.has_perm`` returns
    True for active superusers before any backend runs, so business code must
    call ``core.access.require`` / ``has``, never ``user.has_perm`` (R24).
    """

    def has_perm(self, user_obj, perm, obj=None):
        if not isinstance(obj, Entity):
            return False
        from . import catalogue, services

        if perm not in catalogue.CATALOGUE:
            return False
        return services.has(user_obj, obj, perm)
