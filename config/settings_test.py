"""Test settings: adds the test-only `testdocs` app (sample payment and bill documents)."""
from .settings import *  # noqa: F401,F403

INSTALLED_APPS = [*INSTALLED_APPS, "testdocs"]  # noqa: F405

APPROVAL_ADAPTERS = {
    **APPROVAL_ADAPTERS,  # noqa: F405
    "payments.payment": "testdocs.approval.PaymentApproval",
    "payables.bill": "testdocs.approval.BillApproval",
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
