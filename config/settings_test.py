"""Settings for the test suite: the project settings plus the test-only ``testdocs`` app.

``testdocs`` holds stand-in sub-ledger documents registered with the ledger
document registry (ledger/documents.py). It is never installed in a real
environment, so ``manage.py`` (config.settings) does not know it.
"""
from .settings import *  # noqa: F401,F403
from .settings import INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "testdocs"]

# hr R7: tests use the seeded (unverified) statutory parameters; the AT-4 tests switch this back on.
HR_REQUIRE_VERIFIED_PARAMETERS = False
# A made-up key for the test database only (never a real key).
HR_PERSONAL_DATA_KEYS = ["test-only-hr-personal-data-key-not-secret"]
# A made-up key for banking test data only (never a real key).
BANKING_DATA_KEYS = ["test-only-banking-data-key-not-secret"]
