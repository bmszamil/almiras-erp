"""What billing offers other modules without a user action of its own (billing.md §6).

``contract_allows(contract, purpose)`` is the check invoicing R67 calls before it accepts an advance or deposit
receipt: True only if the contract's approved current version carries that term. It reads a contract the caller
already holds; the caller (invoicing) checks its own user's rights.
"""
from .models import AdvanceTerm, Contract

PURPOSES = ("advance", "deposit")


def contract_allows(contract, purpose):
    if purpose not in PURPOSES:
        raise ValueError(f"Unknown purpose {purpose!r}; expected one of {PURPOSES}.")
    c = Contract.objects.select_related("advance_terms", "deposit_terms", "current_version").get(pk=contract.pk)
    if c.current_version_id is None or c.status not in (Contract.Status.APPROVED, Contract.Status.ACTIVE):
        return False
    if purpose == "advance":
        return c.advance_terms_id is not None and c.advance_terms.recovery in AdvanceTerm.Recovery.values
    return c.deposit_terms_id is not None
