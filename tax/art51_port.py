"""Art. 51 annual true-up hand-off (tax-returns R55; invoicing R104, E25).

``year_status(entity, year)`` tells the VAT return whether the entity recovered ``mixed`` input VAT at a provisional
ratio in ``year`` and, if so, whether the year's final ratio is approved and its true-up posted, with the posted
true-up entries and their signed SAR amounts (+ = more VAT recoverable).

Invoicing R104 (recovery ratios and the ``Art51TrueUp`` document) is **not built**: as built, ``mixed`` input VAT is
posted fully to cost (invoicing R29 default), so nothing is recovered at a provisional ratio and no true-up is due.
``InvoicingArt51Port`` reports exactly that until invoicing implements R104 and replaces it.
"""
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Art51Status:
    applies: bool = False  # mixed VAT was recovered at a provisional ratio in the year
    final_ratio_approved: bool = False
    true_up_posted: bool = False
    true_up_entries: tuple = field(default_factory=tuple)  # ((JournalEntry, Decimal signed SAR), ...)

    @property
    def true_up_vat_sar(self):
        return sum((amount for _, amount in self.true_up_entries), Decimal("0"))


class InvoicingArt51Port:
    """As built: no provisional recovery of mixed VAT, so no true-up (invoicing R29 default)."""

    def year_status(self, entity, year):
        return Art51Status()
