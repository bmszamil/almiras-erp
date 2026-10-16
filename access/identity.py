"""Identity provider seam (R4, D6, D14).

Today: LocalTotpIdentity — Django password plus a confirmed TOTP device (django-otp) for every
human user. Later: a KeycloakIdentity adapter (OIDC authorization code + PKCE, MFA enforced in
Keycloak) replaces it by changing settings.ERP_IDENTITY_ADAPTER; views and the API only call
`identity()`, never django-otp directly. Keycloak is not implemented in this step.
"""
from django.conf import settings
from django.utils.module_loading import import_string
from django_otp import user_has_device


class IdentityAdapter:
    name = "abstract"

    def is_second_factor_verified(self, request, user):
        raise NotImplementedError

    def has_second_factor(self, user):
        raise NotImplementedError

    def user_from_claims(self, claims):
        """Map identity-provider claims (Keycloak: sub, preferred_username, email) to an access.User."""
        raise NotImplementedError


class LocalTotpIdentity(IdentityAdapter):
    name = "local_totp"

    def is_second_factor_verified(self, request, user):
        verified = getattr(user, "is_verified", None)
        return bool(verified and verified())

    def has_second_factor(self, user):
        return user_has_device(user, confirmed=True)


def identity():
    return import_string(settings.ERP_IDENTITY_ADAPTER)()
