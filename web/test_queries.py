"""Query counts do not grow with the rows of a page (web.md R37, R38; WT-14)."""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from web.conftest import draft_invoice, entry

pytestmark = pytest.mark.django_db
LIST_MAX, DASHBOARD_MAX = 15, 25


def count(client, url):
    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url).status_code == 200
    return len(ctx.captured_queries)


def test_wt14_list_and_dashboard_queries_are_flat_and_within_limits(w, signed_in, monkeypatch):
    monkeypatch.setattr("web.middleware.SEEN_RESOLUTION", 10**9)  # no session write in between
    client = signed_in(w.acc)
    client.get("/e/ALM/")  # first request after sign-in writes the session clocks
    for _ in range(2):  # ALM: 5 journal entries, 4 sales documents
        entry(w.alm, w.acc)
    for _ in range(2):
        draft_invoice(w.alm, w.clerk, w.customer)
    urls = ["/e/ALM/ledger/entries/", "/e/ALM/sales/documents/", "/e/ALM/"]
    small = {url: count(client, url) for url in urls}
    for _ in range(45):  # 50 journal entries, 49 sales documents: still one page
        entry(w.alm, w.acc)
        draft_invoice(w.alm, w.clerk, w.customer)
    large = {url: count(client, url) for url in urls}
    assert small == large
    assert large["/e/ALM/ledger/entries/"] <= LIST_MAX and large["/e/ALM/sales/documents/"] <= LIST_MAX
    assert large["/e/ALM/"] <= DASHBOARD_MAX


def test_detail_queries_do_not_grow_with_lines(w, signed_in, monkeypatch):
    monkeypatch.setattr("web.middleware.SEEN_RESOLUTION", 10**9)
    client = signed_in(w.acc)
    client.get("/e/ALM/")
    two = entry(w.alm, w.acc)
    many = entry(w.alm, w.acc, lines=[{"account": "1100", "debit": 1}] * 9 + [{"account": "3100", "credit": 9}])
    assert count(client, f"/e/ALM/ledger/entries/{two.pk}/") == count(client, f"/e/ALM/ledger/entries/{many.pk}/")
