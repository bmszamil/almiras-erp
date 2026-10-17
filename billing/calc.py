"""Pure billing arithmetic (billing.md R14 to R18, R38, R42): periods, the terms in force on a day, proration.

Nothing here reads or writes the database or checks rights; ``billing.services`` loads the rows, checks the
caller's rights and passes plain values in. All money is ``Decimal``, rounded half-up to the contract currency's
decimals (2 or 3, D11), once per run of days with the same amount (R42, OQ-24).
"""
import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext

ZERO = Decimal("0")
ONE_DAY = timedelta(days=1)
MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}
ROLLING_MONTHS = 13  # R10: open-ended contracts are scheduled 13 months ahead


def quantum(places):
    return Decimal(1).scaleb(-places)


def round_money(amount, places):
    """ROUND_HALF_UP to ``places`` decimals (R16, R37 amended)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return amount.quantize(quantum(places), rounding=ROUND_HALF_UP)


def has_places(amount, places):
    return amount == amount.quantize(quantum(places)) if amount.is_finite() else False


def add_months(day, months):
    """``day`` moved by ``months``; the day of month is clamped to the month's length (Jan 31 + 1 = Feb 28)."""
    month0 = day.month - 1 + months
    year, month = day.year + month0 // 12, month0 % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def full_period(day, frequency, anchor, anchor_date):
    """The full billing period (start, end inclusive) that contains ``day`` (R17).

    ``calendar``: months, quarters (Jan/Apr/Jul/Oct), halves (Jan/Jul) or years. ``contract_start``: periods of the
    frequency counted from ``anchor_date`` (the contract start, or the start of a later schedule segment).
    """
    months = MONTHS[frequency]
    if anchor == "calendar":
        first = (day.month - 1) // months * months + 1
        start = date(day.year, first, 1)
        return start, add_months(start, months) - ONE_DAY
    if day < anchor_date:
        raise ValueError("day before the anchor date")
    k = ((day.year - anchor_date.year) * 12 + day.month - anchor_date.month) // months
    start = add_months(anchor_date, k * months)
    if start > day:
        k -= 1
        start = add_months(anchor_date, k * months)
    end = add_months(anchor_date, (k + 1) * months) - ONE_DAY
    while end < day:  # clamped month ends (anchor on the 31st)
        k += 1
        start, end = add_months(anchor_date, k * months), add_months(anchor_date, (k + 1) * months) - ONE_DAY
    return start, end


def bill_date(timing, period_start, period_end):
    """R14: advance → the period start; arrears → the day after the period end."""
    return period_start if timing == "advance" else period_end + ONE_DAY


def days(start, end):
    return (end - start).days + 1


def factor(period_start, period_end, full_days):
    with localcontext() as ctx:
        ctx.prec = 30
        return (Decimal(days(period_start, period_end)) / Decimal(full_days)).quantize(Decimal("0.000001"),
                                                                                         rounding=ROUND_HALF_UP)


# --- terms ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LineTerms:
    line_key: str
    line_no: int
    amount: Decimal
    steps: tuple = ()  # ((effective_from, new_amount), ...) sorted by date
    window: tuple | None = None  # secondment lines: (start, end or None) of the assignment (R19)
    attrs: dict = field(default_factory=dict, compare=False, hash=False)

    def amount_on(self, period_start):
        """R18: the line amount, or the latest step with ``effective_from ≤ period_start`` (OQ-4: next period)."""
        value = self.amount
        for when, new_amount in self.steps:
            if when <= period_start:
                value = new_amount
        return value

    def in_window(self, day):
        if self.window is None:
            return True
        start, end = self.window
        return start <= day and (end is None or day <= end)


@dataclass(frozen=True)
class VersionTerms:
    version_id: int
    version_no: int
    kind: str  # original / amendment / termination / cancellation
    effective_date: date
    end_date: date | None
    billing_frequency: str
    billing_timing: str
    period_anchor: str
    lines: tuple = ()  # LineTerms

    @property
    def bills_nothing(self):
        return self.kind in ("termination", "cancellation")

    def line(self, line_key):
        for line in self.lines:
            if line.line_key == line_key:
                return line
        return None


def governing(versions, day):
    """R38: the approved version with the latest ``effective_date ≤ day`` (equal dates: higher version_no)."""
    best = None
    for v in versions:
        if v.effective_date <= day and (best is None or (v.effective_date, v.version_no) > (best.effective_date,
                                                                                            best.version_no)):
            best = v
    return best


def latest(versions):
    """The version whose terms apply last (latest effective date, then version number)."""
    return max(versions, key=lambda v: (v.effective_date, v.version_no)) if versions else None


def billable_end(contract_start, versions, *, terminated_on=None, cancelled=False, secondment_windows=None):
    """Last billable day of the contract, None when open-ended.

    Cancellation: the day before the start (nothing billable). Termination: ``terminated_on``. Otherwise the end
    date of the latest terms. A secondment contract whose every assignment has an end date ends with the last of
    them (R19); an open assignment keeps it open.
    """
    if cancelled:
        return contract_start - ONE_DAY
    ends = []
    last = latest([v for v in versions if not v.bills_nothing])
    if last is not None and last.end_date is not None:
        ends.append(last.end_date)
    if terminated_on is not None:
        ends.append(terminated_on)
    if secondment_windows:
        if all(end is not None for _, end in secondment_windows):
            ends.append(max(end for _, end in secondment_windows))
    return min(ends) if ends else None


def billable(day, contract_start, version, line, end):
    """A day counts for a line when the contract runs, the governing terms bill and include the line (R19, R40)."""
    if day < contract_start or (end is not None and day > end) or version is None or version.bills_nothing:
        return False
    if version.end_date is not None and day > version.end_date:
        return False
    return line is not None and line.in_window(day)


def required_nets(period_start, period_end, full_days, versions, contract_start, end, places):
    """R42: per line_key, Σ over the parts of the period governed by each version of
    ``round(amount × billable days / full period days, places)``. Also used to price a new invoice, so an
    invoice and its later corrections are computed the same way.

    Consecutive version segments in which the line's amount is unchanged are one part: rounding happens only
    where the amount changes (OQ-24 "rounded per segment" read as per *amount* segment). So an amendment that
    changes no amount (e.g. ``due_days`` only) never moves a cent: a full month is still the monthly amount, and
    an already-billed period needs no correction. AT-25 still holds: 10,000.00 × 15/31 and 12,000.00 × 16/31 are
    rounded separately.

    Returns ``{line_key: (net, governing LineTerms of the last billable day)}`` for every line key that appears in
    any governing version of the period (net may be 0).
    """
    segments = []  # [(version, [days])]
    day = period_start
    while day <= period_end:
        v = governing(versions, day)
        if segments and segments[-1][0] is v:
            segments[-1][1].append(day)
        else:
            segments.append((v, [day]))
        day += ONE_DAY
    keys = []
    for v, _ in segments:
        for line in (v.lines if v else ()):
            if line.line_key not in keys:
                keys.append(line.line_key)
    result = {}
    for key in keys:
        parts = []  # [[amount, billable days]]: consecutive billable segments with the same amount merged
        shown = None
        for v, seg_days in segments:
            line = v.line(key) if v else None
            count = sum(1 for d in seg_days if billable(d, contract_start, v, line, end))
            if count:
                amount = line.amount_on(period_start)
                if parts and parts[-1][0] == amount:
                    parts[-1][1] += count
                else:
                    parts.append([amount, count])
                shown = line
            elif line is not None and shown is None:
                shown = line
        net = ZERO
        for amount, count in parts:
            with localcontext() as ctx:
                ctx.prec = 60
                net += round_money(amount * Decimal(count) / Decimal(full_days), places)
        result[key] = (net, shown)
    return result


# --- schedule -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class PlannedPeriod:
    period_start: date
    period_end: date
    full_days: int
    bill_date: date
    proration_factor: Decimal


def schedule(contract_start, versions, end, horizon):
    """The periods of the contract (R10, R17): from the start to ``end`` (inclusive) or, open-ended, to
    ``horizon``. Frequency, timing and anchor come from the terms in force at each period's start; a change of
    them starts a new segment (R39 allows it only after the last generated period), so a period never spans two
    sets of schedule terms.
    """
    last_day = end if end is not None else horizon
    if not versions or last_day is None or last_day < contract_start:
        return []
    billing_versions = [v for v in versions if not v.bills_nothing]
    if not billing_versions:
        return []
    periods = []
    day = contract_start
    seg_anchor = contract_start
    seg_terms = None
    while day <= last_day:
        v = governing(billing_versions, day) or billing_versions[0]
        terms = (v.billing_frequency, v.billing_timing, v.period_anchor)
        if seg_terms is not None and terms != seg_terms:
            seg_anchor = day
        seg_terms = terms
        frequency, timing, anchor = terms
        full_start, full_end = full_period(day, frequency, anchor, seg_anchor)
        # the next change of schedule terms inside this period cuts it
        cut = full_end
        for other in billing_versions:
            if day < other.effective_date <= full_end and (
                    other.billing_frequency, other.billing_timing, other.period_anchor) != terms:
                cut = min(cut, other.effective_date - ONE_DAY)
        # open-ended: whole periods up to the one that contains the horizon (never a partial one at the horizon)
        p_start, p_end = max(full_start, day), (min(cut, last_day) if end is not None else cut)
        full_days = days(full_start, full_end)
        periods.append(PlannedPeriod(p_start, p_end, full_days, bill_date(timing, p_start, p_end),
                                     factor(p_start, p_end, full_days)))
        day = p_end + ONE_DAY
    return periods


def horizon_for(today, contract_start):
    """R10: open-ended contracts are scheduled ``ROLLING_MONTHS`` months ahead of today (or of the start)."""
    return add_months(max(today, contract_start), ROLLING_MONTHS) - ONE_DAY


def period_text(start, end):
    """Supply-period text appended to invoice line descriptions (3.2)."""
    return f" ({start.isoformat()} to {end.isoformat()})", f" (من {start.isoformat()} إلى {end.isoformat()})"
