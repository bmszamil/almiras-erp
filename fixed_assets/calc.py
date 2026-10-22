"""Pure depreciation arithmetic (fixed-assets.md R10 to R13, R16, R19, R25, R33). No database access.

``schedule(...)`` returns the depreciation per calendar month of one asset, from its start (available-for-use date,
or the day after the cut-over for a migrated asset) until the end of its life, its stop date (write-off) or ``until``:

- straight line: (carrying amount − residual) spread evenly over the remaining life in months; first and last months
  are prorated by days (actual days / days in month, OQ-3 default); each month is rounded half up to 2 dp and the
  month in which the life ends absorbs the residue, so the total is carrying − residual exactly (R13);
- reducing balance: the month's opening carrying amount × annual rate / 12 (prorated by days), never below the
  residual value (R10).

Changes take effect on the first day of a month: a change of estimate (new residual, method, total life or rate) and a
carrying-amount change (impairment, its reversal, a cost adjustment) re-spread what is left over the remaining life
(prospective, IAS 8.36; R16, R19).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")
TWELVE = Decimal("12")
STRAIGHT_LINE = "straight_line"
REDUCING_BALANCE = "reducing_balance"


def q(amount):
    """Half up to 2 dp (R13, R39)."""
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def month_start(d):
    return d.replace(day=1)


def days_in_month(d):
    return calendar.monthrange(d.year, d.month)[1]


def month_end(d):
    return d.replace(day=days_in_month(d))


def next_month(d):
    return (month_end(d) + timedelta(days=1)).replace(day=1)


def add_months(d, months):
    """``d`` plus ``months`` calendar months (day clamped to the month's last day)."""
    total = d.year * 12 + (d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def add_years(d, years):
    return add_months(d, 12 * years)


def month_units(start, end):
    """Months between ``start`` (inclusive) and ``end`` (exclusive), partial months as days / days in month."""
    if end <= start:
        return ZERO
    units, m = ZERO, month_start(start)
    while m < end:
        lo, hi = max(start, m), min(end, next_month(m))
        units += Decimal((hi - lo).days) / Decimal(days_in_month(m))
        m = next_month(m)
    return units


@dataclass(frozen=True)
class Change:
    """Something that changes depreciation from ``on`` (the first day of a month)."""

    on: date
    carrying_delta: Decimal = ZERO  # -impairment, +reversal, +/- cost adjustment
    residual: Decimal | None = None
    method: str | None = None
    life_end: date | None = None  # straight line: new end of life (exclusive)
    annual_rate: Decimal | None = None


@dataclass
class Month:
    start: date  # first day of the calendar month
    days: int  # days depreciated in the month
    amount: Decimal
    opening: Decimal  # carrying amount before this month's depreciation
    last: bool = False  # the month the life ends in (absorbed the residue)


@dataclass
class Basis:
    start: date
    carrying: Decimal
    residual: Decimal
    method: str
    life_end: date | None = None
    annual_rate: Decimal | None = None
    changes: list = field(default_factory=list)
    stop: date | None = None  # last day depreciated (write-off date), inclusive


def schedule(basis, *, until=None):
    """The :class:`Month` list of ``basis`` through the month of ``until`` (inclusive; None: to the end)."""
    out = []
    carrying, residual, method = Decimal(basis.carrying), Decimal(basis.residual), basis.method
    life_end, rate = basis.life_end, basis.annual_rate
    changes = sorted(basis.changes, key=lambda c: c.on)
    start = basis.start
    exact = None
    if method == STRAIGHT_LINE and life_end is not None:
        units = month_units(start, life_end)
        exact = (carrying - residual) / units if units else ZERO
    m = month_start(start)
    last_month = month_start(until) if until is not None else None
    guard = 0
    while True:
        guard += 1
        if guard > 2400 or (last_month is not None and m > last_month):
            break
        respread = False
        while changes and month_start(changes[0].on) <= m:
            c = changes.pop(0)
            carrying += c.carrying_delta
            if c.residual is not None:
                residual = Decimal(c.residual)
            if c.method:
                method = c.method
            if c.life_end is not None:
                life_end = c.life_end
            if c.annual_rate is not None:
                rate = c.annual_rate
            respread = True
        if respread and method == STRAIGHT_LINE and life_end is not None:
            units = month_units(max(start, m), life_end)
            exact = (carrying - residual) / units if units > 0 else ZERO
        if basis.stop is not None and m > basis.stop:
            break
        lo = max(start, m)
        hi = next_month(m)
        if method == STRAIGHT_LINE and life_end is not None:
            hi = min(hi, life_end)
        if basis.stop is not None:
            hi = min(hi, basis.stop + timedelta(days=1))
        days = (hi - lo).days
        room = carrying - residual
        if room <= 0:
            break
        if days <= 0:
            if method == STRAIGHT_LINE and life_end is not None and m >= month_start(life_end):
                break
            m = next_month(m)
            continue
        dim = Decimal(days_in_month(m))
        last = False
        if method == STRAIGHT_LINE:
            ends_here = life_end is not None and life_end <= next_month(m) and (
                basis.stop is None or basis.stop + timedelta(days=1) >= life_end)
            if ends_here:
                amount, last = room, True
            else:
                amount = q(exact * Decimal(days) / dim)
        else:
            amount = q(carrying * Decimal(rate or 0) / TWELVE * Decimal(days) / dim)
        amount = max(ZERO, min(amount, room))
        out.append(Month(m, days, amount, carrying, last))
        carrying -= amount
        if last or carrying <= residual:
            break
        if basis.stop is not None and basis.stop < next_month(m):
            break
        m = next_month(m)
    return out


def total_through(months, through):
    """Sum of the months of ``months`` up to the month of ``through`` (inclusive)."""
    end = month_start(through)
    return sum((x.amount for x in months if x.start <= end), ZERO)


def retention_until(*, retention_class, acquisition_date, life_months, movable_months, vat_return_period="quarterly"):
    """R33 (a), zatca R-38 as corrected by compliance S1 (TIR §8.3.3; Needs expert sign-off: tax).

    Real estate: end of the tax period of acquisition + 15 years. Movable: acquisition + min(Art. 52 movable setting,
    accounting life) months + 5 years, never earlier than the standard 6 years (acquisition + 6 years)."""
    if acquisition_date is None:
        return None
    if retention_class == "capital_asset_real_estate":
        end = month_end(acquisition_date)
        if vat_return_period == "quarterly":
            quarter_end_month = ((acquisition_date.month - 1) // 3 + 1) * 3
            end = month_end(acquisition_date.replace(day=1, month=quarter_end_month))
        return month_end(add_years(end.replace(day=1), 15))
    months = movable_months if not life_months else min(movable_months, life_months)
    computed = add_years(add_months(acquisition_date, months), 5)
    return max(computed, add_years(acquisition_date, 6))


def adjustment_period_end(*, retention_class, acquisition_date, life_months, movable_months, real_estate_months):
    """R46 (a): acquisition + the Art. 52 adjustment period (movable: the shorter of the setting and the life)."""
    if acquisition_date is None:
        return None
    if retention_class == "capital_asset_real_estate":
        return add_months(acquisition_date, real_estate_months)
    months = movable_months if not life_months else min(movable_months, life_months)
    return add_months(acquisition_date, months)
