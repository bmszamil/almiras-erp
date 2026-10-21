from django.apps import AppConfig


class BankingConfig(AppConfig):
    """Banking (docs/specs/banking.md). Registers BankTransaction with the ledger document registry (3.6).

    The document builds its own lines (``build_lines``), every entry for it is checked (``validate_lines``) and
    submitted documents block closing their period (``pending_documents``). No ``creator_may_post``: the creator,
    submitter and last modifier never post it (access R35).
    """

    name = "banking"
    verbose_name = "Banking / البنوك"

    def ready(self):
        from ledger import documents, restricted

        from . import checks  # noqa: F401  (registers banking.E001)
        from . import services
        from .models import BankTransaction

        documents.register(
            BankTransaction, post_permissions={"banking.approve_bank_transaction"},
            reverse_permissions={"banking.reverse_bank_transaction"},
            build_lines=services._bank_transaction_lines, validate_lines=services._check_bank_transaction_lines,
            pending_documents=services._submitted_bank_transactions,
        )
        # invoicing D31b review fix 1: the bank transactions that may touch the purification account's GL 1140
        restricted.allow(services.PURIFICATION_GL, document_type="banking.banktransaction",
                         predicate=services._purification_gl_allowed)

        from . import approval_adapters

        approval_adapters.register()  # access R61: the module's documents go through the one approval engine
