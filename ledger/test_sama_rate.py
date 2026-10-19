"""SAMA rate for VAT in SAR (ledger-fx R12 amendment, AT-41; owner decision G7): ``get_sama_rate``, the
``NonPublicationDay`` reference data and SAMA rows entered by hand or by CSV. Anonymised, illustrative rates."""
import io
from datetime import date
from decimal import Decimal

import pytest
from django.utils import timezone

from core.access.testing import make_user, member
from ledger import services
from ledger.models import ExchangeRate, LedgerError, NonPublicationDay
from ledger.test_fx import rate, setup_entity

pytestmark = pytest.mark.django_db
D = Decimal
THU, FRI, SAT, SUN, MON = (date(2027, 3, d) for d in (4, 5, 6, 7, 8))


@pytest.fixture
def g():
    alm = setup_entity("ALM", "parent")
    treasury, clerk = make_user("treasury"), make_user("clerk")
    member(treasury, alm, "group_treasury")
    member(clerk, alm, "accountant")
    return type("G", (), {"alm": alm, "treasury": treasury, "clerk": clerk})


def sama(on, value, currency="USD"):
    return rate(currency, "SAR", on, value, source="sama", source_reference=f"SAMA bulletin {on}")


def closed(*days):
    user = make_user(f"npd{NonPublicationDay.objects.count()}")
    for day in days:
        NonPublicationDay.objects.create(date=day, name_en="Weekend", name_ar="عطلة نهاية الأسبوع", created_by=user,
                                         verified_by=user, verified_at=timezone.now())


def test_at41_sama_rate_only_across_non_publication_days(g):
    row = sama(THU, "3.7500")
    closed(FRI, SAT)
    rate("USD", "SAR", SUN, "3.7600")  # manual: never a SAMA rate
    found = services.get_sama_rate("USD", SAT, user=g.clerk, entity=g.alm)
    assert (found.rate, found.row) == (D("3.7500"), row)
    for on in (SUN, MON):  # 03-07 is a publication day without a SAMA row
        with pytest.raises(LedgerError) as exc:
            services.get_sama_rate("USD", on, user=g.clerk, entity=g.alm)
        assert exc.value.code == "sama_rate_missing"
        text = " ".join(exc.value.messages)
        assert "USD" in text and str(on) in text and "البنك المركزي" in text
    # functional bookings keep the 5-day fallback of R12 and the manual-first order
    assert services.get_rate(g.alm, "USD", "SAR", MON, user=g.clerk).rate == D("3.7600")


def test_sama_rate_on_the_day_itself_wins(g):
    sama(THU, "3.7500")
    today = sama(FRI, "3.7510")
    closed(FRI)  # a row on a recorded closed day is still that day's row
    assert services.get_sama_rate("USD", FRI, user=g.clerk, entity=g.alm).row == today


def test_sama_rate_ignores_entity_rows_other_sources_and_average_rates(g):
    rate("USD", "SAR", THU, "3.70", entity=g.alm, source="manual")
    rate("USD", "SAR", THU, "3.71", source="file")
    rate("USD", "SAR", THU, "3.72", source="ecb")
    rate("USD", "SAR", THU, "3.73", source="sama", rate_type="average", period_start=THU, period_end=THU,
         source_reference="x")
    with pytest.raises(LedgerError) as exc:
        services.get_sama_rate("USD", THU, user=g.clerk, entity=g.alm)
    assert exc.value.code == "sama_rate_missing"


def test_sama_rate_has_no_five_day_tolerance(g):
    sama(date(2027, 3, 1), "3.75")
    closed(date(2027, 3, 2))  # 03-03 is a publication day: the 03-01 row is not used on 03-03 or later
    with pytest.raises(LedgerError):
        services.get_sama_rate("USD", date(2027, 3, 3), user=g.clerk, entity=g.alm)
    assert services.get_sama_rate("USD", date(2027, 3, 2), user=g.clerk, entity=g.alm).rate == D("3.75")


def test_sama_rate_for_sar_is_one(g):
    assert services.get_sama_rate("SAR", THU, user=g.clerk, entity=g.alm).rate == D("1")


def test_sama_rate_needs_the_right_to_view_rates(g):
    from core.access.services import AccessDenied

    sama(THU, "3.75")
    with pytest.raises(AccessDenied):
        services.get_sama_rate("USD", THU, user=make_user("outsider"), entity=g.alm)


def test_non_publication_day_service_needs_group_treasury_and_both_names(g):
    day = services.create_non_publication_day(user=g.treasury, on=FRI, name_en="Weekend", name_ar="عطلة",
                                              group=g.alm)
    assert day.date == FRI and day.created_by == g.treasury
    with pytest.raises(LedgerError) as exc:
        services.create_non_publication_day(user=g.treasury, on=SAT, name_en="Weekend", name_ar=" ", group=g.alm)
    assert exc.value.code == "name_required"
    with pytest.raises(LedgerError) as exc:
        services.create_non_publication_day(user=g.treasury, on=FRI, name_en="Again", name_ar="مكرر", group=g.alm)
    assert exc.value.code == "duplicate_day"
    from core.access.services import AccessDenied

    with pytest.raises(AccessDenied):
        services.create_non_publication_day(user=g.clerk, on=SUN, name_en="Holiday", name_ar="عطلة", group=g.alm)


def test_create_rate_sama_rows_are_global_with_a_reference(g):
    with pytest.raises(LedgerError) as exc:
        services.create_rate(user=g.treasury, from_currency="USD", to_currency="SAR", on=THU, rate="3.75",
                             source="sama", group=g.alm)
    assert exc.value.code == "sama_reference_required"
    row = services.create_rate(user=g.treasury, from_currency="USD", to_currency="SAR", on=THU, rate="3.75",
                               source="sama", source_reference="SAMA 2027-03-04", group=g.alm)
    assert row.entity_id is None and row.source == "sama"
    with pytest.raises(LedgerError) as exc:
        services.create_rate(user=g.clerk, entity=g.alm, from_currency="USD", to_currency="SAR", on=FRI,
                             rate="3.75", source="sama", source_reference="x")
    assert exc.value.code == "sama_rate_global"


def test_csv_import_of_sama_rows(g):
    text = ("date,from_currency,to_currency,rate,source_reference\n"
            "2027-03-04,USD,SAR,3.7500,SAMA bulletin 2027-03-04\n")
    result = services.import_rates(io.StringIO(text), user=g.treasury, source="sama", group=g.alm)
    assert result["created"] == 1
    row = ExchangeRate.objects.get(source="sama")
    assert (row.entity_id, row.source_reference, row.rate) == (None, "SAMA bulletin 2027-03-04", D("3.75"))
    assert services.import_rates(io.StringIO(text), user=g.treasury, source="sama", group=g.alm)["created"] == 0
    missing = "date,from_currency,to_currency,rate\n2027-03-05,USD,SAR,3.7500\n"
    with pytest.raises(LedgerError) as exc:
        services.import_rates(io.StringIO(missing), user=g.treasury, source="sama", group=g.alm)
    assert "source reference" in " ".join(exc.value.messages)
    with pytest.raises(LedgerError) as exc:
        services.import_rates(io.StringIO(text), user=g.clerk, source="sama", entity=g.alm)
    assert exc.value.code == "sama_rate_global"
    with pytest.raises(LedgerError) as exc:
        services.import_rates(io.StringIO(text), user=g.treasury, source="ecb", group=g.alm)
    assert exc.value.code == "source_not_enabled"
