"""Owner decision H1 (ledger-fx R12 amendment, AT-44): the proposed 2026-2027 SAMA non-publication days, loaded
unverified by ``ledger_load_non_publication_days`` and verified by a second person before ``get_sama_rate`` bridges
across them. Anonymised users and illustrative rates."""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core import access
from core.access.models import AccessLog
from core.access.services import AccessDenied
from core.access.testing import make_agent, make_user, member
from ledger import non_publication_seed as seed
from ledger import services
from ledger.models import LedgerError, NonPublicationDay
from ledger.test_fx import rate, setup_entity

D = Decimal


@pytest.fixture
def g(db):
    alm = setup_entity("ALM", "parent")
    loader, checker, clerk = make_user("loader"), make_user("checker"), make_user("clerk")
    member(loader, alm, "group_treasury")
    member(checker, alm, "group_treasury")
    member(clerk, alm, "accountant")
    return type("G", (), {"alm": alm, "loader": loader, "checker": checker, "clerk": clerk})


def _days(year, weekdays):
    day, out = date(year, 1, 1), set()
    while day.year == year:
        if day.weekday() in weekdays:
            out.add(day)
        day += timedelta(days=1)
    return out


# --- the seed file --------------------------------------------------------------------------------------------

def test_seed_file_matches_the_rules():
    assert seed.load_seed_file() == seed.proposed_days((2026, 2027))


def test_seed_has_every_friday_and_saturday_and_the_ksa_holidays_of_2026_and_2027():
    rows = {date.fromisoformat(r["date"]): r for r in seed.load_seed_file()}
    weekend = _days(2026, {4, 5}) | _days(2027, {4, 5})
    assert weekend <= set(rows)
    assert {d.year for d in rows} == {2026, 2027}
    holidays = {d for d in rows if d not in weekend}
    founding_national = {date(y, m, d) for y in (2026, 2027) for m, d in ((2, 22), (9, 23))}
    # Umm al-Qura (hijridate): 29 Ramadan 1447 = 2026-03-18, 9 Dhu al-Hijjah 1447 = 2026-05-26; 1448: 03-08, 05-15
    eids = {date(2026, 3, 19) + timedelta(days=i) for i in range(4)} | {date(2026, 5, 26) + timedelta(days=i)
                                                                          for i in range(4)}
    eids |= {date(2027, 3, 9) + timedelta(days=i) for i in range(4)} | {date(2027, 5, 15) + timedelta(days=i)
                                                                          for i in range(4)}
    assert holidays == (founding_national | eids) - weekend
    assert rows[date(2026, 2, 22)]["name_ar"] == "يوم التأسيس"
    assert rows[date(2027, 9, 23)]["name_en"] == "National Day"
    assert rows[date(2026, 3, 20)]["name_en"] == "Eid al-Fitr holiday 1447 AH (Friday)"  # holiday on a weekend day
    assert rows[date(2027, 5, 18)]["name_ar"] == "إجازة عيد الأضحى 1448هـ"
    for row in rows.values():
        assert row["name_en"] and row["name_ar"]
        assert row["source"].startswith("proposed — confirm against the SAMA/official announcement")
        assert "مقترح" in row["source"] and len(row["source"]) <= 300


# --- loading ----------------------------------------------------------------------------------------------------

def test_command_is_a_dry_run_by_default(g, capsys):
    call_command("ledger_load_non_publication_days", "--user", "loader")
    assert NonPublicationDay.objects.count() == 0
    out = capsys.readouterr().out
    assert "dry run: 224 new day(s)" in out and "--apply" in out


def test_command_apply_loads_every_row_unverified_and_logs_it(g, capsys):
    call_command("ledger_load_non_publication_days", "--user", "loader", "--group", "ALM", "--apply")
    days = NonPublicationDay.objects.all()
    assert days.count() == len(seed.load_seed_file())
    assert not days.filter(verified_at__isnull=False).exists() and not days.exclude(created_by=g.loader).exists()
    assert all(d.source.startswith("proposed") for d in days) and "(unverified" in str(days.first())
    log = AccessLog.objects.filter(actor=g.loader, details__action="load_non_publication_days").get()
    assert log.details["created"] == days.count() and log.entity == g.alm
    capsys.readouterr()
    call_command("ledger_load_non_publication_days", "--user", "loader", "--apply")  # re-run: nothing new
    assert "applied: 0 new day(s)" in capsys.readouterr().out
    assert NonPublicationDay.objects.count() == days.count()


def test_recorded_dates_are_never_changed_by_the_load(g):
    own = services.create_non_publication_day(user=g.checker, on=date(2026, 9, 23), name_en="National Day 96",
                                              name_ar="اليوم الوطني 96", group=g.alm)
    new, skipped = services.load_non_publication_days(seed.load_seed_file(), user=g.loader, apply=True)
    assert skipped == [date(2026, 9, 23)] and date(2026, 9, 23) not in new
    own.refresh_from_db()
    assert (own.name_en, own.created_by, own.is_verified) == ("National Day 96", g.checker, True)


def test_loading_needs_a_person_with_the_global_rate_right(g):
    with pytest.raises(CommandError):
        call_command("ledger_load_non_publication_days", "--user", "clerk", "--apply")
    with pytest.raises(CommandError):
        call_command("ledger_load_non_publication_days", "--user", "nobody")
    system = access.system_user("fx_import")
    member(system, g.alm, "system_fx_import")
    agent = make_agent()
    for user in (g.clerk, system, agent):
        with pytest.raises(AccessDenied):
            services.load_non_publication_days(seed.load_seed_file(), user=user, apply=True)
    assert NonPublicationDay.objects.count() == 0


def test_malformed_seed_rows_are_refused_before_anything_is_written(g, tmp_path):
    good = {"date": "2026-01-02", "name_en": "Weekend", "name_ar": "عطلة", "source": "proposed"}
    for rows in ([good, {**good, "date": "2026-01-03", "name_ar": ""}], [good, good],
                 [{**good, "date": "02/01/2026"}], [{**good, "source": ""}], ["2026-01-02"]):
        with pytest.raises(LedgerError) as exc:
            services.load_non_publication_days(rows, user=g.loader, apply=True)
        assert exc.value.code == "seed_row_invalid" and "السطر" in " ".join(exc.value.messages)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"date": "2026-01-02"}), encoding="utf-8")
    with pytest.raises(CommandError):
        call_command("ledger_load_non_publication_days", "--user", "loader", "--file", str(bad), "--apply")
    assert NonPublicationDay.objects.count() == 0


# --- verification and the SAMA lookup -------------------------------------------------------------------------

def _usd_thursday_and_loaded_weekend(g):
    rate("USD", "SAR", date(2026, 1, 1), "3.7500", source="sama", source_reference="SAMA 2026-01-01")
    rows = [r for r in seed.load_seed_file() if r["date"] in ("2026-01-02", "2026-01-03")]
    services.load_non_publication_days(rows, user=g.loader, apply=True)
    return NonPublicationDay.objects.order_by("date")


def test_unverified_days_never_bridge_to_an_earlier_sama_rate(g):
    _usd_thursday_and_loaded_weekend(g)
    with pytest.raises(LedgerError) as exc:
        services.get_sama_rate("USD", date(2026, 1, 3), user=g.clerk, entity=g.alm)
    text = " ".join(exc.value.messages)
    assert exc.value.code == "sama_rate_missing"
    assert "not yet verified: 2026-01-02, 2026-01-03" in text and "لم يُتحقق منها" in text


def test_a_second_person_verifies_and_the_days_then_bridge(g):
    fri, sat = _usd_thursday_and_loaded_weekend(g)
    services.verify_non_publication_day(fri, user=g.checker)
    with pytest.raises(LedgerError):  # Saturday still unverified
        services.get_sama_rate("USD", date(2026, 1, 3), user=g.clerk, entity=g.alm)
    assert services.get_sama_rate("USD", date(2026, 1, 2), user=g.clerk, entity=g.alm).rate == D("3.7500")
    done = services.verify_non_publication_day(sat, user=g.checker, group=g.alm)
    assert (done.verified_by, done.is_verified) == (g.checker, True)
    assert services.get_sama_rate("USD", date(2026, 1, 3), user=g.clerk, entity=g.alm).rate == D("3.7500")
    log = AccessLog.objects.filter(actor=g.checker, details__action="verify_non_publication_day")
    assert log.count() == 2 and log.first().details["day"] in ("2026-01-02", "2026-01-03")


def test_the_loader_cannot_verify_and_a_day_is_verified_once(g):
    fri, sat = _usd_thursday_and_loaded_weekend(g)
    with pytest.raises(AccessDenied) as exc:
        services.verify_non_publication_day(fri, user=g.loader)
    assert exc.value.code == "self_approval"
    with pytest.raises(AccessDenied):
        services.verify_non_publication_day(fri, user=g.clerk)  # no global rate right
    services.verify_non_publication_day(fri, user=g.checker)
    with pytest.raises(LedgerError) as exc:
        services.verify_non_publication_day(fri, user=g.checker)
    assert exc.value.code == "already_verified"
    fri.refresh_from_db()
    assert fri.verified_by == g.checker


def test_a_day_entered_by_hand_is_verified_by_its_entry(g):
    day = services.create_non_publication_day(user=g.loader, on=date(2026, 1, 2), name_en="Weekend", name_ar="عطلة",
                                              group=g.alm, source="SAMA notice 12")
    assert (day.verified_by, day.is_verified, day.source) == (g.loader, True, "SAMA notice 12")


# --- migration 0019 ----------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_0019_marks_days_entered_before_it_as_verified_by_their_creator():
    def migrate(targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    leaf = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = migrate([("ledger", "0018_sama_non_publication_days")])
    try:
        User = old.get_model("auth", "User")
        user = User.objects.create(username="entered-by")
        Day = old.get_model("ledger", "NonPublicationDay")
        day = Day.objects.create(date=date(2027, 3, 5), name_en="Weekend", name_ar="عطلة", created_by=user)
    finally:
        new = migrate(leaf)
    stored = new.get_model("ledger", "NonPublicationDay").objects.get(pk=day.pk)
    assert (stored.verified_by_id, stored.verified_at, stored.source) == (user.pk, stored.created_at, "")
