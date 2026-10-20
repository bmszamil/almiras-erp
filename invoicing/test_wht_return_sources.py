"""OQ-30 (payroll R43): other modules register their withholding tax rows for the monthly WHT return
(``register_wht_return_source``); the rows join invoicing's own and the 2220 tie-out covers that module's document
type. Payroll's end-to-end case is payroll/test_payments.py::test_oq30_r43_..."""
from datetime import date
from decimal import Decimal

import pytest

from invoicing import services


@pytest.fixture
def registry():
    saved = dict(services._WHT_RETURN_SOURCES)
    yield services._WHT_RETURN_SOURCES
    services._WHT_RETURN_SOURCES.clear()
    services._WHT_RETURN_SOURCES.update(saved)


def test_registered_source_rows_join_the_return_and_the_tie_out(w, registry):
    seen = []

    def rows(entity, start, end):
        seen.append((entity.code, start, end))
        return [{"category": "zz-test", "return_line_code": "L9", "vendor": "EMP-1", "country": "EG",
                 "rate": Decimal("5.00"), "treaty_certificate": None, "treaty_country": "", "base_sar": Decimal("100.00"),
                 "wht_sar": Decimal("5.00"), "gross_up": False, "source": "testsource"}]

    services.register_wht_return_source("testdocs.whtsource", rows)
    services.register_wht_return_source("testdocs.whtsource", rows)  # idempotent per document type
    data = services.wht_return_data(w.alm, date(2027, 3, 15), user=w.accountant)
    assert seen == [("ALM", date(2027, 3, 1), date(2027, 3, 31))]
    assert [(r["vendor"], r["wht_sar"], r["source"]) for r in data["rows"]] == [("EMP-1", Decimal("5.00"), "testsource")]
    # Nothing of that document type is on 2220 in the books: the difference shows it.
    assert data["total_wht_sar"] == Decimal("5.00") and data["ledger_2220_sar"] == 0 and data["difference"] == Decimal("5.00")
