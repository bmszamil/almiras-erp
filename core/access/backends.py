"""Authentication backends (access.md appendix, R3, R20, R21)."""
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend, ModelBackend

from core.models import Entity

from .models import Kind


class HumanModelBackend(ModelBackend):
    """Django's username/password login, for human users only.

    System users (unusable password) and agent users (gateway tokens only)
    can never sign in through a view, the admin or a session (R20, R21).
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        """As ModelBackend, but the password hasher runs once whatever the account (web.md review, R12).

        ModelBackend returns quickly for an unusable password (system users) and hashes for everyone else, so the
        response time told agent and system usernames apart. Here an unknown user, an unusable password and a
        refused kind (agent, system, inactive) each run the hasher once on a throwaway user, like a real check.
        """
        UserModel = get_user_model()
        if username is None:
            username = kwargs.get(UserModel.USERNAME_FIELD)
        if username is None or password is None:
            return None
        try:
            user = UserModel._default_manager.get_by_natural_key(username)
        except UserModel.DoesNotExist:
            UserModel().set_password(password)
            return None
        if not user.has_usable_password() or not self.user_can_authenticate(user):
            UserModel().set_password(password)
            return None
        return user if user.check_password(password) else None

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
