"""Control accounts: the one list every module uses (invoicing R97, policy A; migration R8, R22; dry run gap 7).

Read from the group chart template (``ledger/coa_template.py``, flags ``control`` and ``tax_control``), so a change is
made in one place and every entity's chart follows it:

- ``SUBLEDGER_CONTROL_CODES``: trade receivables 1200, due from group entities 1210, trade payables 2100, due to group
  entities 2110. Their balance is the sum of the open customer or vendor items in invoicing (R39); only invoicing
  documents (and the migration opening entry, M6) post to them. 2310 and 2320, named in earlier drafts of policy A,
  are not in the chart and are not control accounts; when 2310 is added (invoicing R99) it carries the ``control``
  flag (invoicing OQ-81, team default).
- ``TAX_CONTROL_CODES``: VAT input 1310, VAT output 2200, withholding tax payable 2220. Documents set them; a person
  never types them on a document line. They are not sub-ledger control accounts: their opening balance is evidenced
  against the last VAT and withholding-tax returns (migration W15), not against open items.

The purification account 1140 is a *restricted* account (``ledger.restricted``), not a control account.
"""
from .coa_template import TEMPLATE

SUBLEDGER_CONTROL_CODES = frozenset(code for code, _en, _ar, _type, flags in TEMPLATE if flags.get("control"))
TAX_CONTROL_CODES = frozenset(code for code, _en, _ar, _type, flags in TEMPLATE if flags.get("tax_control"))


def is_subledger_control(account):
    """True for a sub-ledger control account (by code, the same in every entity's chart)."""
    return getattr(account, "code", account) in SUBLEDGER_CONTROL_CODES


def is_tax_control(account):
    return getattr(account, "code", account) in TAX_CONTROL_CODES
