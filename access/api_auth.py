"""API authentication and permission.

- Humans: session sign-in through the OTP-verified admin login (password + TOTP, D14). Every API
  call checks the second factor, not only the admin.
- Service users (agents, integrations): revocable, expiring bearer tokens (R4), issued and revoked
  through access.services (audited). They never use passwords or sessions.
"""
from rest_framework import exceptions, permissions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

from .identity import identity
from .models import ServiceToken
from .services import _token_hash, issue_service_token, revoke_service_token  # noqa: F401  (re-exported)


class ServiceTokenAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate(self, request):
        auth = get_authorization_header(request).split()
        if not auth or auth[0].lower() != self.keyword:
            return None
        if len(auth) != 2:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        try:
            key = auth[1].decode()
        except UnicodeDecodeError:
            raise exceptions.AuthenticationFailed("Invalid token header.")
        token = ServiceToken.objects.select_related("user").filter(key_hash=_token_hash(key)).first()
        if token is None or not token.is_valid or not token.user.is_active or token.user.is_human:
            raise exceptions.AuthenticationFailed("Invalid, expired or revoked token.")
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
