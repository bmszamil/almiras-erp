"""The ledger's adapter to the one approval engine (access.md §10.5, R61): manual journal entries.

A manual draft is approved by ``ledger.post_entry``: the engine issues the single-use grant for that draft
(``core.access.require``) and ``ledger.services.post_entry`` posts it with every Sharia, period, balance and entity
check (R7). Registered in ``LedgerConfig.ready``.
"""


def _approve(entry, user):
    from core import access

    from . import services

    grant = access.require(user, entry.entity, "ledger.post_entry", document=entry)
    return services.post_entry(entry, grant)


def register():
    from core.access import engine

    from .models import JournalEntry

    engine.register(JournalEntry, "ledger.manual_entry", permission="ledger.post_entry",
                    submit_permission="ledger.create_entry", amount=lambda d: None,
                    # Last change, else submission, else creation: an entry submitted without an edit still has a
                    # version (web.md R7; found by the adapter-version test, final verification of 7a104f1, item 6).
                    version=lambda d: engine.version_text(d.modified_at or d.submitted_at or d.created_at or ""),
                    label=lambda d: (f"Journal entry {d.number or '#' + str(d.pk)}",
                                     f"قيد يومية {d.number or '#' + str(d.pk)}"),
                    approve=_approve)
