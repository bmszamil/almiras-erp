"""API authentication, permission and error handling.

- Humans: session sign-in through the OTP-verified admin login (password + TOTP, D14). Every API
  call checks the second factor, not only the admin.
- Service users (agents, integrations): revocable bearer tokens (R4). They never use passwords.
- Errors: {code, message_en, message_ar}; 403/404/409/422 (spec §6.1).
"""
import hashlib
import secrets

from django.db import transaction
from django.utils import timezone
from rest_framework import exceptions, permissions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from .errors import BilingualError
from .identity import identity
from .models import ServiceToken, User


def _hash(key):
    return hashlib.sha256(key.encode()).hexdigest()


@transaction.atomic
def issue_service_token(user, label="", expires_at=None):
    """Return the plain token once; only its hash is stored."""
    if user.is_human:
        raise BilingualError("token_for_human", "Tokens are for service users only.", "الرموز لمستخدمي الخدمة فقط.")
    key = secrets.token_urlsafe(32)
    ServiceToken.objects.create(user=user, key_hash=_hash(key), label=label, expires_at=expires_at)
    return key


def revoke_service_token(token):
    token.revoked_at = timezone.now()
    token.save(update_fields=["revoked_at"])


class ServiceTokenAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword:
            return None
        if len(auth) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        token = ServiceToken.objects.select_related("user").filter(key_hash=_hash(auth[1].decode())).first()
        if token is None or not token.is_valid or not token.user.is_active or token.user.is_human:
            raise exceptions.AuthenticationFailed("Invalid or revoked token.")
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"


class IsTwoFactorAuthenticated(permissions.BasePermission):
    """D14: every human API call needs a session verified with the second factor."""

    message = "Two-factor authentication required."

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if isinstance(request.auth, ServiceToken):
            return not user.is_human
        if not user.is_human:
            return False  # service users never use sessions
        return identity().is_second_factor_verified(request._request, user)


__all__ = ["ServiceTokenAuthentication", "IsTwoFactorAuthenticated", "issue_service_token", "User"]
