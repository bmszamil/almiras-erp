"""Pure billing arithmetic (R14 to R18, R38, R42): periods, proration, the terms of a day, required nets."""
from datetime import date
from decimal import Decimal

import pytest

from billing import calc

D = Decimal


def v(no, eff, *, kind="original", end=date(2027, 12, 31), freq="monthly", timing="advance", anchor="calendar",
      lines=(("k1", "10000.00"),), steps=()):
    return calc.VersionTerms(no, no, kind, eff, end, freq, timing, anchor,
                             tuple(calc.LineTerms(key, i + 1, D(amount), tuple(steps), None, {})
                                   for i, (key, amount) in enumerate(lines)))


def test_full_periods_calendar_and_contract_start():
    assert calc.full_period(date(2027, 1, 15), "monthly", "calendar", None) == (date(2027, 1, 1), date(2027, 1, 31))
    assert calc.full_period(date(2027, 5, 2), "quarterly", "calendar", None) == (date(2027, 4, 1), date(2027, 6, 30))
    assert calc.full_period(date(2027, 8, 2), "semiannual", "calendar", None) == (date(2027, 7, 1), date(2027, 12, 31))
    assert calc.full_period(date(2027, 8, 2), "annual", "calendar", None) == (date(2027, 1, 1), date(2027, 12, 31))
    start = date(2027, 1, 15)
    assert calc.full_period(date(2027, 2, 20), "monthly", "contract_start", start) == (date(2027, 2, 15),
                                                                                       date(2027, 3, 14))
    assert calc.full_period(date(2027, 2, 28), "monthly", "contract_start", date(2027, 1, 31)) == (
        date(2027, 2, 28), date(2027, 3, 30))


def test_r14_bill_dates():
    assert calc.bill_date("advance", date(2027, 3, 1), date(2027, 3, 31)) == date(2027, 3, 1)
    assert calc.bill_date("arrears", date(2027, 3, 1), date(2027, 3, 31)) == date(2027, 4, 1)


def test_r16_r17_first_partial_period_by_actual_days():
    periods = calc.schedule(date(2027, 1, 15), [v(1, date(2027, 1, 15))], date(2027, 3, 31), None)
    first = periods[0]
    assert (first.period_start, first.period_end, first.full_days) == (date(2027, 1, 15), date(2027, 1, 31), 31)
    assert first.proration_factor == D("0.548387")
    nets = calc.required_nets(first.period_start, first.period_end, 31, [v(1, date(2027, 1, 15))], date(2027, 1, 15),
                              date(2027, 3, 31), 2)
    assert nets["k1"][0] == D("5483.87")  # AT-5: 10,000.00 x 17/31, half-up


def test_r17_contract_start_anchor_only_last_period_partial():
    periods = calc.schedule(date(2027, 1, 15), [v(1, date(2027, 1, 15), anchor="contract_start", end=date(2027, 4, 20))],
                            date(2027, 4, 20), None)
    assert [(p.period_start, p.period_end) for p in periods] == [
        (date(2027, 1, 15), date(2027, 2, 14)), (date(2027, 2, 15), date(2027, 3, 14)),
        (date(2027, 3, 15), date(2027, 4, 14)), (date(2027, 4, 15), date(2027, 4, 20))]
    assert periods[-1].full_days == 30


def test_r18_price_step_from_next_period():
    steps = ((date(2027, 7, 15), D("11000.00")),)
    versions = [v(1, date(2027, 1, 1), steps=steps)]
    july = calc.required_nets(date(2027, 7, 1), date(2027, 7, 31), 31, versions, date(2027, 1, 1), None, 2)
    august = calc.required_nets(date(2027, 8, 1), date(2027, 8, 31), 31, versions, date(2027, 1, 1), None, 2)
    assert (july["k1"][0], august["k1"][0]) == (D("10000.00"), D("11000.00"))


def test_r38_terms_of_a_day_latest_effective_then_version_no():
    v1, v2, v3 = v(1, date(2027, 1, 1)), v(2, date(2027, 3, 1)), v(3, date(2027, 3, 1))
    assert calc.governing([v1, v2, v3], date(2027, 2, 28)) is v1
    assert calc.governing([v1, v2, v3], date(2027, 3, 1)) is v3


def test_r42_segments_rounded_per_version():
    versions = [v(1, date(2027, 1, 1)), v(2, date(2027, 3, 16), lines=(("k1", "12000.00"),))]
    nets = calc.required_nets(date(2027, 3, 1), date(2027, 3, 31), 31, versions, date(2027, 1, 1), None, 2)
    assert nets["k1"][0] == D("4838.71") + D("6193.55") == D("11032.26")  # AT-25


def test_r40_termination_and_cancellation_bill_nothing_after():
    versions = [v(1, date(2027, 1, 1)), v(2, date(2027, 1, 16), kind="termination", lines=())]
    end = calc.billable_end(date(2027, 1, 1), versions, terminated_on=date(2027, 1, 15))
    nets = calc.required_nets(date(2027, 1, 1), date(2027, 1, 31), 31, versions, date(2027, 1, 1), end, 2)
    assert nets["k1"][0] == D("4838.71")  # AT-24
    cancelled = [v(1, date(2027, 1, 1)), v(2, date(2027, 1, 1), kind="cancellation", lines=())]
    end = calc.billable_end(date(2027, 1, 1), cancelled, cancelled=True)
    # the cancellation governs every day and has no lines: nothing is required (the pass credits all that was billed)
    assert calc.required_nets(date(2027, 1, 1), date(2027, 1, 31), 31, cancelled, date(2027, 1, 1), end, 2) == {}
    assert calc.schedule(date(2027, 1, 1), cancelled, end, None) == []


def test_r37_three_decimals_rounded_half_up():
    versions = [v(1, date(2027, 1, 15), lines=(("k1", "1000.000"),))]
    nets = calc.required_nets(date(2027, 1, 15), date(2027, 1, 31), 31, versions, date(2027, 1, 15), None, 3)
    assert nets["k1"][0] == D("548.387")  # AT-37


def test_r19_secondment_window():
    line = calc.LineTerms("k1", 1, D("31000.00"), (), (date(2027, 1, 1), date(2027, 3, 10)), {})
    versions = [calc.VersionTerms(1, 1, "original", date(2027, 1, 1), None, "monthly", "arrears", "calendar", (line,))]
    end = calc.billable_end(date(2027, 1, 1), versions, secondment_windows=[line.window])
    assert end == date(2027, 3, 10)
    nets = calc.required_nets(date(2027, 3, 1), date(2027, 3, 10), 31, versions, date(2027, 1, 1), end, 2)
    assert nets["k1"][0] == D("10000.00")  # AT-7 / B3


def test_r68_frequency_change_starts_a_new_segment():
    versions = [v(1, date(2027, 1, 1)), v(2, date(2027, 4, 1), freq="quarterly")]
    periods = calc.schedule(date(2027, 1, 1), versions, date(2027, 12, 31), None)
    assert [(p.period_start, p.period_end) for p in periods][2:] == [
        (date(2027, 3, 1), date(2027, 3, 31)), (date(2027, 4, 1), date(2027, 6, 30)),
        (date(2027, 7, 1), date(2027, 9, 30)), (date(2027, 10, 1), date(2027, 12, 31))]


def test_rolling_horizon_13_months():
    assert calc.horizon_for(date(2027, 1, 10), date(2027, 1, 1)) == date(2028, 2, 9)


@pytest.mark.parametrize("value,places,ok", [(D("1.23"), 2, True), (D("1.234"), 2, False), (D("1.234"), 3, True),
                                             (D("1.2345"), 3, False)])
def test_places(value, places, ok):
    assert calc.has_places(value, places) is ok


def test_r10_open_ended_schedule_has_whole_periods_up_to_the_horizon():
    versions = [v(1, date(2027, 1, 1), end=None)]
    periods = calc.schedule(date(2027, 1, 1), versions, None, calc.horizon_for(date(2027, 1, 10), date(2027, 1, 1)))
    assert (periods[-1].period_start, periods[-1].period_end) == (date(2028, 2, 1), date(2028, 2, 29))
    assert len(periods) == 14 and all(p.proration_factor == 1 for p in periods)
