"""RTL/LTR rendering, language switch, bilingual data, money and status display, masking, lists and empty states
(web.md R24, R27 to R29, R33 to R35, R37; WT-12, WT-27 display part). Fixture data only."""
import re
from datetime import date
from decimal import Decimal

import pytest
from django.template import Context, Template
from django.utils import translation

from core.access.models import AccessLog
from core.models import Currency
from web.conftest import entry
from web.templatetags.web_tags import format_amount

pytestmark = pytest.mark.django_db


def html_tag(body):
    return re.search(r"<html[^>]*>", body).group(0)


def test_wt12_arabic_profile_renders_rtl_then_the_switch_saves_english(w, signed_in):
    client = signed_in(w.acc)  # profile language "ar" (fixture)
    body = client.get("/e/ALM/").content.decode()
    assert html_tag(body) == '<html lang="ar" dir="rtl">'
    assert "bootstrap.rtl.min.css" in body and "almiras-logo-ar-on-navy.svg" in body
    assert "almiras-logo-en" not in body  # never both logos (brand.md)
    assert '<bdi dir="ltr" class="alm-ident">ALM</bdi>' in body  # R29
    response = client.post("/language/", {"language": "en", "next": "/e/ALM/"})
    assert response.status_code == 302 and response["Location"] == "/e/ALM/"
    w.acc.access_profile.refresh_from_db()
    assert w.acc.access_profile.preferred_language == "en"
    row = AccessLog.objects.get(event="profile_changed", actor=w.acc)
    assert row.details == {"field": "preferred_language", "before": "ar", "after": "en"}
    body = client.get("/e/ALM/").content.decode()
    assert html_tag(body) == '<html lang="en" dir="ltr">'
    assert "bootstrap.min.css" in body and "bootstrap.rtl" not in body and "almiras-logo-en-on-navy.svg" in body
    assert "Dashboard" in body and "لوحة المعلومات" not in body


def test_r27_before_sign_in_cookie_then_accept_language_then_arabic(w, client):
    assert html_tag(client.get("/login/").content.decode()) == '<html lang="ar" dir="rtl">'  # default
    english = client.get("/login/", HTTP_ACCEPT_LANGUAGE="en-GB,en;q=0.9,ar;q=0.5").content.decode()
    assert html_tag(english) == '<html lang="en" dir="ltr">' and "almiras-logo-en.svg" in english
    response = client.post("/language/", {"language": "ar", "next": "/login/"}, HTTP_ACCEPT_LANGUAGE="en")
    assert response.cookies["alm_lang"].value == "ar" and response.cookies["alm_lang"]["httponly"]
    arabic = client.get("/login/", HTTP_ACCEPT_LANGUAGE="en").content.decode()
    assert html_tag(arabic) == '<html lang="ar" dir="rtl">' and "almiras-logo-ar.svg" in arabic
    assert not AccessLog.objects.filter(event="profile_changed").exists()  # nobody signed in


def test_language_switch_is_post_only_and_refuses_unknown_languages(w, client):
    assert client.get("/language/").status_code == 405
    assert client.post("/language/", {"language": "fr"}).status_code == 400


def test_wt12_sign_in_uses_the_profile_language_not_the_cookie(w, client):
    client.cookies["alm_lang"] = "en"
    client.post("/login/", {"username": "acc", "password": "made-up-test-password-7Q!"})
    assert html_tag(client.get("/e/ALM/").content.decode()) == '<html lang="ar" dir="rtl">'


@pytest.mark.parametrize("lang", ["ar", "en"])
def test_template_smoke_every_page_renders_in_both_languages(w, signed_in, lang):
    w.acc.access_profile.preferred_language = lang
    w.acc.access_profile.save(update_fields=["preferred_language"])
    client = signed_in(w.acc)
    urls = ["/e/ALM/", "/e/ALM/ledger/entries/", f"/e/ALM/ledger/entries/{w.alm_posted.pk}/",
            f"/e/ALM/ledger/entries/{w.alm_draft.pk}/", "/e/ALM/sales/documents/",
            f"/e/ALM/sales/documents/{w.alm_invoice.pk}/", f"/e/ALM/sales/documents/{w.alm_person_invoice.pk}/",
            "/e/ALM/ledger/entries/?status=posted", "/e/ALM/sales/documents/?status=draft&doc_type=invoice"]
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
        body = response.content.decode()
        assert html_tag(body) == (f'<html lang="{lang}" dir="{"rtl" if lang == "ar" else "ltr"}">'), url
        assert 'href="#main"' in body and 'id="main"' in body, url  # skip link (R35)
        assert "Traceback" not in body and "{%" not in body and "{{" not in body, url


def test_login_and_error_pages_render_without_a_user(w, client):
    assert client.get("/login/").status_code == 200
    missing = client.get("/no-such-page/")
    assert missing.status_code == 404 and "Not found" in missing.content.decode()


def test_journal_entry_detail_shows_lines_totals_and_status_as_text(w, signed_in):
    body = signed_in(w.acc).get(f"/e/ALM/ledger/entries/{w.alm_posted.pk}/").content.decode()
    assert w.alm_posted.number in body
    assert '<bdi dir="ltr" class="alm-amount">1,250.50&nbsp;<span class="alm-currency">SAR</span></bdi>' in body
    assert 'alm-status--posted' in body and ">مرحّل</span>" in body  # status as text, not colour only (R35)
    assert "1100" in body and "3100" in body


def test_r35_every_status_badge_carries_its_text(w, signed_in):
    body = signed_in(w.acc).get("/e/ALM/ledger/entries/").content.decode()
    badges = re.findall(r'<span class="alm-status alm-status--(\w+)">'
                        r'<span class="alm-status__shape" aria-hidden="true">[^<]+</span>([^<]*)</span>', body)
    assert badges and all(text.strip() for _, text in badges)
    assert {"مسودة", "مرحّل"} <= {text for _, text in badges}


def test_r24_individual_buyer_is_masked_in_list_and_detail_and_nothing_is_logged(w, signed_in):
    from invoicing import services as invoicing
    from invoicing.conftest import final

    client = signed_in(w.acc)  # accountant holds invoicing.view_personal_data, still masked on GET pages
    listing = client.get("/e/ALM/sales/documents/").content.decode()
    assert "Made Up Person" not in listing and "شخص مختلق" not in listing and "P001" in listing
    assert "مخفي" in listing and "***" not in listing  # the active half of the mask only
    assert "شركة المشتري التجريبية" in listing  # business names are not personal data
    draft = client.get(f"/e/ALM/sales/documents/{w.alm_person_invoice.pk}/").content.decode()
    assert "Made Up Person" not in draft and "شخص مختلق" not in draft and "تُحسب عند الإصدار" not in draft
    assert "تُحدَّد المجاميع والمبالغ بالريال وبيانات المشتري عند إصدار المستند." in draft
    final(w, invoicing.submit_sales_document(w.alm_person_invoice, user=w.clerk))  # snapshot written at issue
    issued = client.get(f"/e/ALM/sales/documents/{w.alm_person_invoice.pk}/").content.decode()
    assert "Made Up Person" not in issued and "شخص مختلق" not in issued and "Buyer Street" not in issued
    assert "البيانات الشخصية للمشتري الفرد مخفية" in issued
    assert not AccessLog.objects.filter(event__in=["personal_data_viewed", "personal_data_exported"]).exists()


def test_totals_are_not_shown_as_zero_before_issue(w, signed_in):
    listing = signed_in(w.acc).get("/e/ALM/sales/documents/").content.decode()
    assert listing.count("تُحسب عند الإصدار") == 2 and "0.00" not in listing


def test_business_buyer_detail_shows_the_buyer_with_identifiers_in_bdi(w, signed_in):
    from invoicing import services as invoicing

    from invoicing.conftest import final

    final(w, w.alm_invoice)
    body = signed_in(w.acc).get(f"/e/ALM/sales/documents/{w.alm_invoice.pk}/").content.decode()
    doc = invoicing.sales_documents(w.acc, w.alm).get(pk=w.alm_invoice.pk)
    assert doc.number and f'<bdi dir="ltr" class="alm-ident">{doc.number}</bdi>' in body
    assert '<bdi dir="ltr" class="alm-ident">310000000000003</bdi>' in body  # buyer VAT number (R29)
    assert "1,150.00" in body  # gross with 15 % VAT


def test_lists_filter_by_enumerated_values_and_ignore_invalid_ones(w, signed_in):
    client = signed_in(w.acc)
    posted = client.get("/e/ALM/ledger/entries/?status=posted").content.decode()
    assert "ALM posted entry" in posted and "ALM rent accrual" not in posted
    bad = client.get("/e/ALM/ledger/entries/?status=nonsense&date_from=not-a-date").content.decode()
    assert "بعض عوامل التصفية غير صالحة" in bad and "ALM rent accrual" in bad
    none = client.get("/e/ALM/ledger/entries/?date_from=2030-01-01").content.decode()
    assert "لا توجد نتائج مطابقة لعوامل التصفية." in none
    sales = client.get("/e/ALM/sales/documents/?status=submitted").content.decode()
    assert "C001" in sales and "P001" not in sales


def test_r37_lists_are_paginated_and_keep_their_filters(w, signed_in, monkeypatch):
    import web.views

    monkeypatch.setattr(web.views, "PAGE_SIZE", 2)
    client = signed_in(w.acc)
    first = client.get("/e/ALM/ledger/entries/?status=draft").content.decode()
    assert "الصفحة 1 من 1" not in first
    entry(w.alm, w.acc, "third draft")
    first = client.get("/e/ALM/ledger/entries/?status=draft").content.decode()
    assert "الصفحة 1 من 2" in first and "?status=draft&amp;page=2" in first
    second = client.get("/e/ALM/ledger/entries/?status=draft&page=2").content.decode()
    assert "الصفحة 2 من 2" in second and "page=1" in second


def test_empty_states(w, signed_in):
    client = signed_in(w.inact_acc)
    dashboard = client.get("/e/INACT/").content.decode()
    assert "لا توجد قيود يومية في هذا الكيان بعد." in dashboard
    assert "لا توجد فواتير أو إشعارات مبيعات في هذا الكيان بعد." in dashboard
    assert "alm-empty__symbol" in dashboard
    assert "لا توجد قيود يومية في هذا الكيان بعد." in client.get("/e/INACT/ledger/entries/").content.decode()


def test_dashboard_counts_come_from_the_entity_only(w, signed_in):
    body = signed_in(w.acc).get("/e/ALM/").content.decode()
    kpis = re.findall(r'<p class="alm-kpi"><bdi dir="ltr">(\d+)</bdi></p>', body)
    assert kpis == ["3", "2"]  # ALM: 3 journal entries, 2 sales documents (SUB's are not counted)


# --- money (R34) ---------------------------------------------------------------------------------------------------

def render(source, **context):
    with translation.override("ar"):
        return Template("{% load web_tags %}" + source).render(Context(context))


def test_r34_money_uses_the_currencys_minor_units_and_decimal_only():
    kwd, sar = Currency.objects.get(code="KWD"), Currency.objects.get(code="SAR")
    assert render("{% money v c %}", v=Decimal("123.456"), c=kwd) == (
        '<bdi dir="ltr" class="alm-amount">123.456&nbsp;<span class="alm-currency">KWD</span></bdi>')
    assert render("{% money v c %}", v=Decimal("1234567.5"), c=sar).startswith(
        '<bdi dir="ltr" class="alm-amount">1,234,567.50&nbsp;')
    assert format_amount(Decimal("10.000"), 2) == "10.00"
    assert format_amount(Decimal("10.125"), 2) == "10.125"  # never rounds a stored amount away
    assert format_amount(Decimal("-5"), 2) == "-5.00"
    with pytest.raises(TypeError):
        format_amount(10.5, 2)


def test_r28_bilingual_value_falls_back_with_its_own_lang():
    class Obj:
        name_en, name_ar = "Only English", ""

    assert render('{% pick o "name" %}', o=Obj()) == '<span lang="en" dir="ltr">Only English</span>'
    Obj.name_ar = "عربي"
    assert render('{% pick o "name" %}', o=Obj()) == "عربي"


def test_r30_dates_are_iso_gregorian_in_bdi():
    assert render("{% day d %}", d=date(2027, 1, 10)) == (
        '<bdi dir="ltr"><time datetime="2027-01-10">2027-01-10</time></bdi>')
