from django.apps import AppConfig


class LedgerConfig(AppConfig):
    name = "ledger"

    def ready(self):
        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
