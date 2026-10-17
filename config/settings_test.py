"""Test settings: adds the test-only `testdocs` app (sample payment and bill documents).

DEBUG stays off (production-like); a throwaway secret key is set before the base settings load.
"""
import os

os.environ.pop("DJANGO_DEBUG", None)  # tests always run production-like, whatever the shell exports
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-not-secret")

from .settings import *  # noqa: E402,F401,F403

INSTALLED_APPS = [*INSTALLED_APPS, "testdocs"]  # noqa: F405

APPROVAL_ADAPTERS = {
    **APPROVAL_ADAPTERS,  # noqa: F405
    "payments.payment": "testdocs.approval.PaymentApproval",
    "payables.bill": "testdocs.approval.BillApproval",
}

SECURE_SSL_REDIRECT = False  # the test client speaks plain HTTP
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
