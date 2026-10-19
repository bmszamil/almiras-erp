"""ledger-fx R51 (owner decision H2): the narrow rule that lets a registered sales document book at its SAMA
tax-point row beyond ``FX_MAX_RATE_AGE_DAYS``. The document flow is tested in
``invoicing/test_sama_tax_point_booking.py``; here the row checks, the registry guard and the caller refusals."""
from datetime import date, timedelta

import pytest
from django.utils import timezone

from core.access.testing import make_user, member
from invoicing.models import PurchaseDocument, SalesDocument
from ledger import documents, services
from ledger.models import LedgerError, NonPublicationDay
from ledger.test_fx import rate, setup_entity

pytestmark = pytest.mark.django_db
THU, FRI, SAT, SUN = (date(2027, 3, d) for d in (4, 5, 6, 7))


def _closed(*days, verified=True):
    user = make_user(f"npd{NonPublicationDay.objects.count()}")
    for day in days:
        NonPublicationDay.objects.create(date=day, name_en="Weekend", name_ar="عطلة", created_by=user,
                                         verified_by=user if verified else None,
                                         verified_at=timezone.now() if verified else None)


def _sama(on=THU, **kw):
    return rate(kw.pop("f", "USD"), kw.pop("t", "SAR"), on, "3.75", source=kw.pop("source", "sama"),
                source_reference="SAMA bulletin", **kw)


def test_i2_a_sama_row_counts_for_a_tax_point_when_it_is_the_latest_one_on_or_before_it():
    row = _sama()
    assert services._sama_row_problem(row, "USD", "SAR", THU) is None
    _closed(FRI, verified=False)  # non-publication days are information only (I2)
    for on in (FRI, SAT, SUN):  # any gap: Sunday is a publication day without a SAMA row
        assert services._sama_row_problem(row, "USD", "SAR", on) is None
    assert "after" in services._sama_row_problem(row, "USD", "SAR", date(2027, 3, 3))[0]
    newer = _sama(on=SAT)
    assert services._sama_row_problem(row, "USD", "SAR", FRI) is None  # the newer row is after the tax point
    for on in (SAT, SUN):
        en, ar = services._sama_row_problem(row, "USD", "SAR", on)
        assert f"later SAMA row {newer.pk}" in en and "latest" in en and "البنك المركزي" in ar
    assert services._sama_row_problem(row, "USD", "SAR", SUN, bridge=False) is None  # kind and date only
    # as_of: a SAMA row recorded after the document chose its row does not undo it
    before = newer.created_at - timedelta(seconds=1)
    assert services._sama_row_problem(row, "USD", "SAR", SUN, as_of=before) is None
    assert services._sama_row_problem(row, "USD", "SAR", SUN, as_of=newer.created_at) is not None
    assert services._sama_row_problem(newer, "USD", "SAR", SUN) is None


def test_only_a_global_sama_spot_row_into_sar_in_a_sar_entity_qualifies():
    egy = setup_entity("EGY", "full", None, currency="EGP")
    alm = setup_entity("ALM", "parent")
    cases = [
        (rate("USD", "SAR", THU, "3.75", entity=alm, source="sama", source_reference="x"), "USD", "SAR"),
        (rate("USD", "SAR", THU, "3.76"), "USD", "SAR"),  # manual
        (rate("SAR", "USD", THU, "0.2666666667", source="sama", source_reference="x"), "USD", "SAR"),  # inverse
        (rate("USD", "SAR", THU, "3.75", source="sama", source_reference="x", rate_type="average",
              period_start=date(2027, 3, 1), period_end=THU), "USD", "SAR"),
        (_sama(f="EUR", on=THU), "USD", "SAR"),  # another currency
    ]
    for row, code, functional in cases:
        assert services._sama_row_problem(row, code, functional, THU) is not None, row
    good = _sama(on=FRI)
    assert "SAR entity" in services._sama_row_problem(good, "USD", egy.base_currency_id, FRI)[0]
    assert services._sama_row_problem(good, "SAR", "SAR", FRI) is not None


def test_only_sales_documents_with_their_own_build_lines_register_the_hook():
    current = documents.get("invoicing.purchasedocument")
    with pytest.raises(documents.DocumentRegistryError):
        with documents._registered_for_tests(PurchaseDocument, post_permissions=current.post_permissions,
                                             build_lines=current.build_lines, sama_tax_point=lambda d: None):
            pass
    sales = documents.get("invoicing.salesdocument")
    with pytest.raises(documents.DocumentRegistryError):
        with documents._registered_for_tests(SalesDocument, post_permissions=sales.post_permissions,
                                             validate_lines=sales.validate_lines, sama_tax_point=lambda d: None):
            pass
    assert sales.sama_tax_point is not None and documents.SAMA_TAX_POINT_TYPES == {"invoicing.salesdocument"}


def test_a_caller_never_sets_the_rate_source_or_the_sama_row():
    alm = setup_entity("ALM", "parent")
    clerk = make_user("clerk")
    member(clerk, alm, "accountant")
    row = _sama()
    base = {"account": "1100", "currency": "USD", "txn_debit": "100.00"}
    counter = {"account": "4100", "currency": "USD", "txn_credit": "100.00"}
    with pytest.raises(LedgerError) as exc:
        services.create_entry(alm, THU, "x", [{**base, "rate_source": "sama_tax_point"}, counter], user=clerk)
    assert exc.value.code == "rate_source_reserved" and "البنك المركزي" in " ".join(exc.value.messages)
    with pytest.raises(LedgerError) as exc:
        services.create_entry(alm, SUN, "x", [{**base, "rate_source": "sama_tax_point", "exchange_rate": row},
                                              counter], user=clerk)
    assert exc.value.code == "pinned_rate_not_allowed"
