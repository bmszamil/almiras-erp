from django.apps import AppConfig


class TestdocsConfig(AppConfig):
    """Test-only stand-in for the future payables/payments modules. Installed only by config.settings_test."""

    name = "testdocs"
