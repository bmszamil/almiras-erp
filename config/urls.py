"""URL configuration.

The admin is an OTPAdminSite: sign-in needs password + TOTP token (D14), and every admin page
requires an OTP-verified session.
"""
from django.contrib import admin
from django.urls import include, path
from django_otp.admin import OTPAdminSite

from access.views import enrol_totp

admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path("admin/", admin.site.urls),
    path("access/enrol-2fa/", enrol_totp, name="enrol-totp"),  # D21 (b): self-enrolment after a 2FA reset
    path("api/v1/", include("approvals.urls")),
    path("api/v1/intercompany/", include("intercompany.urls")),
]
