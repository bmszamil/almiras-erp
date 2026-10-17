"""URL configuration.

The admin is an OTPAdminSite: sign-in needs password + TOTP token (D14), and every admin page
requires an OTP-verified session.
"""
from django.contrib import admin
from django.urls import include, path
from django_otp.admin import OTPAdminSite

admin.site.__class__ = OTPAdminSite

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("approvals.urls")),
]
