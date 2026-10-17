"""Restricted accounts: GL accounts only some kinds of entry may touch (invoicing R54 amendment D31b, review fix 1).

A restricted account is named by its code (the same code in every entity's chart). An entry with a line on it is
posted only when one registered **allowance** accepts the entry (or, for a reversal, the entry it reverses, up to the
first entry of the chain). Manual journals are never accepted unless an allowance says so. The checks run in
``ledger.services`` for every posting (``post_entry``, ``create_entry(post=True)``, ``reverse_entry``) and through
``validate_entry``; drafts of manual journals with such a line are refused when created or changed, except drafts
marked ``source="import"`` (a migration opening entry is linked to its import record after its draft is created; it
is checked again when posted).

Registered by the apps that own the rules, from their ``AppConfig.ready()``; the registry is frozen afterwards::

    restricted.restrict("1140", name_en=..., name_ar=..., lock=lock_fn, check=check_fn)   # the owner (invoicing)
    restricted.allow("1140", document_type="banking.banktransaction", predicate=fn)        # each document's app
    restricted.allow(None, document_type="", predicate=fn)                                  # every restricted account

- ``lock(entity)`` is called inside the posting transaction, after the entry row is locked and **before** the period
  lock and validation, so postings that touch the account in one entity run one at a time.
- ``check(entry, lines)`` returns problems ("English / العربية") for an otherwise allowed entry (e.g. a balance that
  must not go below zero); it runs under that lock.
- ``predicate(entry)`` receives the first entry of the chain (never a reversal) and returns True to allow it.
  ``document_type=""`` allowances apply to entries that are not a document's posting.
"""
from dataclasses import dataclass
from typing import Callable

from django.apps import apps


class RestrictedAccountError(RuntimeError):
    """A programming error in a restricted-account registration."""


@dataclass(frozen=True)
class RestrictedAccount:
    code: str
    name_en: str
    name_ar: str
    posted_by_en: str
    posted_by_ar: str
    lock: Callable | None = None
    check: Callable | None = None


@dataclass(frozen=True)
class Allowance:
    code: str | None  # None: every restricted account
    document_type: str
    predicate: Callable | None = None


_ACCOUNTS = {}
_ALLOWANCES = []


def _not_frozen():
    if apps.ready:
        raise RestrictedAccountError("Restricted accounts are registered from AppConfig.ready() only.")


def restrict(code, *, name_en, name_ar, posted_by_en, posted_by_ar, lock=None, check=None):
    """``posted_by_en`` / ``posted_by_ar``: which entries may post to it, for the refusal message."""
    _not_frozen()
    if code in _ACCOUNTS:
        raise RestrictedAccountError(f"Account {code} is already restricted.")
    _ACCOUNTS[code] = RestrictedAccount(code, name_en, name_ar, posted_by_en, posted_by_ar, lock, check)


def allow(code, *, document_type, predicate=None):
    _not_frozen()
    _ALLOWANCES.append(Allowance(code, document_type, predicate))


def codes():
    return frozenset(_ACCOUNTS)


def get(code):
    return _ACCOUNTS.get(code)


def root_of(entry):
    """The first entry of a reversal chain (a reversal is allowed when what it reverses was)."""
    seen = set()
    while entry.reversal_of_id is not None and entry.pk not in seen:
        seen.add(entry.pk)
        entry = type(entry).objects.get(pk=entry.reversal_of_id)
    return entry


def allowed(code, entry):
    root = root_of(entry)
    for allowance in _ALLOWANCES:
        if allowance.code not in (None, code) or allowance.document_type != (root.document_type or ""):
            continue
        if allowance.predicate is None or allowance.predicate(root) is True:
            return True
    return False


def refusal(code):
    """The bilingual refusal for an entry no allowance accepts."""
    spec = _ACCOUNTS[code]
    return (f"Account {code} ({spec.name_en}) is posted only by {spec.posted_by_en}; never by a manual journal or any "
            f"other entry. / الحساب {code} ({spec.name_ar}) يُرحَّل إليه من خلال {spec.posted_by_ar} فقط؛ ولا يُرحَّل "
            "إليه بقيد يدوي أو بأي قيد آخر.")
