"""Settings for the test suite: the project settings plus the test-only ``testdocs`` app.

``testdocs`` holds stand-in sub-ledger documents registered with the ledger
document registry (ledger/documents.py). It is never installed in a real
environment, so ``manage.py`` (config.settings) does not know it.
"""
from .settings import *  # noqa: F401,F403
from .settings import INSTALLED_APPS

INSTALLED_APPS = [*INSTALLED_APPS, "testdocs"]
