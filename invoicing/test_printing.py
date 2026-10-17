"""The printable invoice (invoicing R47, R72, R94; AT-37, AT-66, AT-100): bilingual HTML with title, seller CR and
unified number, buyer, lines, VAT breakdown, SAR totals and the ZATCA QR. Made-up identifiers only (R70)."""
from datetime import date

import pytest

from core.access.models import AccessLog
from invoicing import services
from invoicing.conftest import ADDRESS, approve, final, invoice, make_entity, usd_rate
from invoicing.qr_svg import qr_svg

pytestmark = pytest.mark.django_db


def html(w, doc, user=None):
    return services.render_sales_document(doc, user=user or w.clerk)


def note(w, original, **fields):
    doc = services.create_sales_document(w.alm, user=w.clerk, customer=original.customer, issue_date=date(2027, 1, 20),
                                         doc_type="credit_note", original_document=original, supply_date=date(2027, 1, 10),
                                         reason_ar="خصم متفق عليه", payment_means_code="30", **fields)
    services.add_sales_line(doc, user=w.clerk, income_type="rent_commercial", quantity="1", unit_price="100.00",
                            description_ar="خصم")
    return services.submit_sales_document(doc, user=w.clerk)


def test_at37_titles_in_arabic_and_english(w):
    standard = final(w, invoice(w))
    page = html(w, standard)
    assert "فاتورة ضريبية" in page and "Tax Invoice" in page and "Simplified" not in page
    assert 'dir="rtl"' in page and 'lang="ar"' in page
    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="شخص", customer_kind="individual",
                                      **ADDRESS)
    simplified = final(w, invoice(w, customer=person))
    page = html(w, simplified)
    assert "فاتورة ضريبية مبسطة" in page and "Simplified Tax Invoice" in page
    cn = final(w, note(w, standard))
    page = html(w, cn)
    assert "إشعار دائن" in page and "Credit Note" in page and "Tax Invoice" not in page
    assert standard.number in page  # the original's number (BT-25)


def test_at66_seller_cr_and_unified_number_with_bilingual_labels(w):
    page = html(w, final(w, invoice(w)))
    assert "رقم السجل التجاري / <span class=\"ltr\">Commercial Registration No.</span>" in page
    assert "الرقم الوطني الموحد / <span class=\"ltr\">Unified National No.</span>" in page
    assert "1010000001" in page and "7000000001" in page
    only_unified = make_entity("UNI", "full", w.alm, vat="344444444400003", unified="7000000002")
    from core.access.testing import member

    member(w.clerk, only_unified, "receivables_clerk")
    customer = services.create_customer(only_unified, user=w.clerk, code="C9", name_ar="عميل", is_vat_registered=True,
                                        vat_number="310000000000003", **ADDRESS)
    draft = services.create_sales_document(only_unified, user=w.clerk, customer=customer, issue_date=date(2027, 1, 10))
    page = html(w, draft)
    assert "7000000002" in page and "Commercial Registration No." not in page


def test_r47_lines_vat_breakdown_sar_totals_and_qr(w):
    usd_rate(w, "3.75")
    doc = final(w, invoice(w, [("rent_commercial", "1", "1000.00")], currency="USD"))
    page = html(w, doc)
    for text in ("بند اختبار", "Test line", "1000.00", "150.00", "1150.00", "562.50", "4312.50", "USD", "SAR",
                 "Total VAT in SAR", "إجمالي الضريبة بالريال"):
        assert text in page, text
    assert "<svg" in page and doc.zatca_qr  # the QR from the ZATCA data (fake port: base64 "fake")
    assert qr_svg(doc.zatca_qr).count("<rect") > 20


def test_r44_issued_but_not_cleared_and_rejected_are_marked(w):
    issued = approve(w, invoice(w))
    page = html(w, issued)
    assert "not to be given to the buyer" in page and "No QR code yet" in page
    rejected = w.zatca.reject(issued)
    assert "Rejected by ZATCA" in html(w, rejected)


def test_at100_entity_without_vat_number_is_never_titled_tax_invoice(w):
    nov = make_entity("NOV", "full", w.alm, cr="1010000077")
    from core.access.testing import member

    member(w.clerk, nov, "receivables_clerk")
    customer = services.create_customer(nov, user=w.clerk, code="C1", name_ar="عميل", **ADDRESS)
    services.add_customer_identifier(customer, "CRN", "1010000099", user=w.clerk)
    doc = services.create_sales_document(nov, user=w.clerk, customer=customer, issue_date=date(2027, 1, 10),
                                         supply_date=date(2027, 1, 10))
    services.add_sales_line(doc, user=w.clerk, income_type="rent_residential", quantity="1", unit_price="100.00",
                            description_ar="إيجار سكني")
    for d in (doc, services.submit_sales_document(doc, user=w.clerk)):
        page = html(w, d)
        assert "Tax Invoice" not in page and "فاتورة ضريبية" not in page
        assert "Invoice" in page and "R94" in page


def test_r51_individual_buyer_masked_without_the_right_and_print_logged(w):
    person = services.create_customer(w.alm, user=w.clerk, code="P2", name_ar="شخص للطباعة", name_en="Print Person",
                                      customer_kind="individual", **ADDRESS)
    doc = final(w, invoice(w, customer=person))
    page = html(w, doc, user=w.viewer)
    assert "Print Person" not in page and "شخص للطباعة" not in page and "مخفي" in page
    assert not AccessLog.objects.filter(event="personal_data_viewed").exists()
    assert "Print Person" in html(w, doc, user=w.clerk)
    row = AccessLog.objects.get(event="personal_data_viewed")
    assert row.details == {"records": [["invoicing.salesdocument", doc.pk]], "how": "print"}


def test_render_needs_invoicing_view(w):
    from core import access

    with pytest.raises(access.AccessDenied):
        services.render_sales_document(final(w, invoice(w)), user=w.sub_clerk)
