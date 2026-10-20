"""Template tags of the web UI (web.md R28, R29, R33, R34, R35).

* ``{% t "key" %}``: a UI text in the active language (web/texts.py).
* ``{% pick obj "name" %}``: a bilingual data value (``name_ar`` / ``name_en``); the other language, marked with its
  own ``lang``, when the active one is empty.
* ``{{ value|ident }}``: an identifier wrapped in ``<bdi dir="ltr">`` (R29).
* ``{% money amount currency %}``: a ``Decimal`` with its currency code and the currency's minor units, never
  through float (R34).
* ``{% day value %}``: a Gregorian date (Hijri display is a later slice, R30).
* ``{% status kind value %}``: a status as text plus a shape, never by colour only (R35).
* ``{% half label %}``: the active half of a bilingual "English / العربية" label (``core.models.label_part``).
"""
from decimal import Decimal

from django import template
from django.utils import translation
from django.utils.html import format_html

from core.models import label_part

from .. import texts

register = template.Library()


def _lang():
    return "ar" if (translation.get_language() or "").startswith("ar") else "en"


@register.simple_tag
def t(key, **values):
    return texts.text(key, _lang(), **values)


@register.simple_tag
def pick(obj, field):
    """``obj.<field>_ar`` in Arabic, ``obj.<field>_en`` in English; the other one (with its ``lang``) when empty."""
    lang = _lang()
    other = "en" if lang == "ar" else "ar"
    value = getattr(obj, f"{field}_{lang}", "") if obj is not None else ""
    if value:
        return value
    fallback = getattr(obj, f"{field}_{other}", "") if obj is not None else ""
    if not fallback:
        return ""
    return format_html('<span lang="{}" dir="{}">{}</span>', other, "rtl" if other == "ar" else "ltr", fallback)


@register.filter
def ident(value):
    """An identifier (number, code, VAT or CR number) that must read left to right also in RTL (R29)."""
    if value in (None, ""):
        return ""
    return format_html('<bdi dir="ltr" class="alm-ident">{}</bdi>', value)


@register.simple_tag
def half(label):
    return label_part(label, _lang())


def format_amount(value, places):
    """``value`` (Decimal or int) with thousands separators and at least ``places`` decimals.

    Never rounds a stored amount away: when it has more decimals than the currency's minor units (it should not),
    every stored decimal is shown. No float anywhere (R34).
    """
    if isinstance(value, float):
        raise TypeError("Money must be Decimal, never float (CLAUDE.md rule 7).")
    amount = value if isinstance(value, Decimal) else Decimal(value)
    places = int(places)
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < 0:
        significant = amount.normalize().as_tuple().exponent
        if isinstance(significant, int) and -significant > places:
            places = -significant
    return f"{amount:,.{places}f}"


@register.simple_tag
def money(amount, currency):
    """``1,234.50 SAR`` in an LTR ``<bdi>`` (R29, R34). ``currency`` is a ``core.Currency`` (its minor units)."""
    if amount in (None, ""):
        return ""
    code = getattr(currency, "code", currency) or ""
    places = getattr(currency, "minor_units", 2)
    return format_html('<bdi dir="ltr" class="alm-amount">{}&nbsp;<span class="alm-currency">{}</span></bdi>',
                       format_amount(amount, places), code)


@register.simple_tag
def amount(value, places=2):
    """An amount without currency (the column header names it)."""
    if value in (None, ""):
        return ""
    return format_html('<bdi dir="ltr" class="alm-amount">{}</bdi>', format_amount(value, places))


@register.simple_tag
def day(value):
    """A Gregorian date as ``YYYY-MM-DD`` in ``<time>`` (R30: Hijri display comes in a later slice)."""
    if not value:
        return ""
    iso = value.isoformat()
    return format_html('<bdi dir="ltr"><time datetime="{}">{}</time></bdi>', iso, iso)


@register.simple_tag
def moment(value):
    """A date and time in Asia/Riyadh (R32), ``YYYY-MM-DD HH:MM``."""
    if not value:
        return ""
    from django.utils import timezone

    local = timezone.localtime(value)
    return format_html('<bdi dir="ltr"><time datetime="{}">{}</time></bdi>', local.isoformat(),
                       local.strftime("%Y-%m-%d %H:%M"))


# Status shapes: the text says the status; the shape and colour only add to it (R35).
_SHAPES = {
    "draft": "○", "submitted": "◔", "issued": "◑", "posted": "●", "final": "●", "reversed": "↺", "rejected": "✕",
    "not_submitted": "○", "pending": "◔", "cleared": "●", "reported": "●", "cleared_with_warnings": "◐",
    "reported_with_warnings": "◐",
}


@register.simple_tag
def status(kind, value, label=None):
    """A status badge: shape (hidden from screen readers) + the status text in the active language."""
    if kind == "je":
        text = texts.text(f"je_status.{value}", _lang())
    else:
        text = label_part(label, _lang()) if label else value
    return format_html(
        '<span class="alm-status alm-status--{}"><span class="alm-status__shape" aria-hidden="true">{}</span>{}</span>',
        value, _SHAPES.get(value, "■"), text,
    )


@register.simple_tag(takes_context=True)
def page_url(context, page_number):
    """The current list URL with ``page`` set and every filter kept (filters are enumerated values, R23)."""
    request = context["request"]
    query = request.GET.copy()
    query["page"] = page_number
    return f"?{query.urlencode()}"


@register.simple_tag
def person(user):
    """A person's display name in the active language, else the username (audit fields such as "posted by")."""
    if user is None:
        return ""
    profile = getattr(user, "access_profile", None)
    lang = _lang()
    name = ""
    if profile is not None:
        name = getattr(profile, f"display_name_{lang}", "") or profile.display_name_en or profile.display_name_ar
    return name or user.get_username()
