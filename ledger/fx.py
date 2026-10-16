"""Pure money and rate arithmetic for the multi-currency ledger (ledger-fx.md R7, R8, R17, R40, R41).

No database access and no rights checks: callers in ``ledger.services`` do both.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

from core.models import MAX_MINOR_UNITS

from .models import LedgerError

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
RATE_PLACES = 10
RATE_QUANTUM = Decimal(1).scaleb(-RATE_PLACES)
FUNCTIONAL_PLACES = 2


def error(en, ar, code=None):
    """A LedgerError with both languages (R37)."""
    return LedgerError(f"{en} / {ar}", code=code)


def to_decimal(value, what="amount"):
    """Decimal from a Decimal, int or numeric string. Floats are rejected, not converted (R8, CLAUDE.md rule 7)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise error(
            f"The {what} {value!r} is a float; pass a Decimal or a string.",
            f"القيمة {value!r} ({what}) عدد عشري ثنائي؛ استخدم Decimal أو نصًا.",
            code="float_rejected",
        )
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation:
            raise error(f"{value!r} is not a number ({what}).", f"{value!r} ليس رقمًا ({what}).", code="not_a_number") from None
    else:
        raise error(f"{value!r} is not a number ({what}).", f"{value!r} ليس رقمًا ({what}).", code="not_a_number")
    if not result.is_finite():
        raise error(f"{value!r} is not a finite number ({what}).", f"{value!r} ليس رقمًا محدودًا ({what}).", code="not_a_number")
    return result


def has_places(amount, places):
    """True if ``amount`` uses no more than ``places`` decimals (trailing zeros do not count)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return amount == amount.quantize(Decimal(1).scaleb(-places))


def check_txn_decimals(amount, currency):
    """R8 / R40: a transaction amount uses at most the currency's minor units, and never more than 3."""
    if currency.minor_units > MAX_MINOR_UNITS:
        raise error(
            f"Currency {currency.code} has {currency.minor_units} minor units; at most {MAX_MINOR_UNITS} can be stored.",
            f"للعملة {currency.code} عدد {currency.minor_units} خانات عشرية؛ الحد الأقصى للتخزين {MAX_MINOR_UNITS}.",
            code="too_many_minor_units",
        )
    if not has_places(amount, currency.minor_units):
        raise error(
            f"{currency.code} amounts allow {currency.minor_units} decimals; {amount} has more.",
            f"مبالغ {currency.code} تقبل {currency.minor_units} خانات عشرية؛ المبلغ {amount} يتجاوز ذلك.",
            code="txn_decimals",
        )


def check_functional_decimals(amount):
    """R8: functional debit/credit always have 2 decimals."""
    if not has_places(amount, FUNCTIONAL_PLACES):
        raise error(
            f"Functional amounts have 2 decimals; {amount} has more.",
            f"المبالغ بالعملة الوظيفية بخانتين عشريتين؛ المبلغ {amount} يتجاوز ذلك.",
            code="functional_decimals",
        )


def check_rate(rate):
    """A rate is above zero with at most 10 decimals (3.3)."""
    if rate <= ZERO:
        raise error("An exchange rate must be above zero.", "يجب أن يكون سعر الصرف أكبر من صفر.", code="rate_not_positive")
    if not has_places(rate, RATE_PLACES):
        raise error(
            f"An exchange rate has at most {RATE_PLACES} decimals; {rate} has more.",
            f"لسعر الصرف {RATE_PLACES} خانات عشرية كحد أقصى؛ السعر {rate} يتجاوز ذلك.",
            code="rate_decimals",
        )


def round_functional(amount):
    """ROUND_HALF_UP to 2 decimals (R17 default, OQ-8)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def convert(txn_amount, rate):
    """R7 / R41: functional amount = round_half_up(txn_amount x rate, 2), computed without intermediate rounding."""
    with localcontext() as ctx:
        ctx.prec = 60
        return (txn_amount * rate).quantize(CENT, rounding=ROUND_HALF_UP)


def inverse(rate):
    """1 / rate rounded to 10 decimals (R12)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return (ONE / rate).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)


def implied_rate(functional_amount, txn_amount):
    """Functional units per transaction unit, rounded to 10 decimals (R20 actual bank rate)."""
    with localcontext() as ctx:
        ctx.prec = 60
        return (functional_amount / txn_amount).quantize(RATE_QUANTUM, rounding=ROUND_HALF_UP)
