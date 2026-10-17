"""Compliance check of slice 1 (docs/compliance/invoicing-slice1-compliance-2026-10-17.md), rows fixed in code."""
from datetime import date

import pytest

from invoicing import services
from invoicing.conftest import ADDRESS, D, approve, final, invoice
from invoicing.models import IncomeType, InvoicingError, NoteReason, VatExemptionReason

pytestmark = pytest.mark.django_db

# XML Implementation Standard v1.2 §11.2.4 (pp. 31-33), as published (downloaded 2026-10-16).
ZATCA_VATEX = {
    "VATEX-SA-29": ("Financial services mentioned in Article 29 of the VAT Regulations", "الخدمات المالية"),
    "VATEX-SA-29-7": ("Life insurance services mentioned in Article 29 of the VAT Regulations", "عقد تأمين على الحياة"),
    "VATEX-SA-30": ("Real estate transactions mentioned in Article 30 of the VAT Regulations",
                    "التوريدات العقارية المعفاة من الضريبة"),
    "VATEX-SA-32": ("Export of goods", "صادرات السلع من المملكة"),
    "VATEX-SA-33": ("Export of services", "صادرات الخدمات من المملكة"),
    "VATEX-SA-34-1": ("The international transport of Goods", "النقل الدولي للسلع"),
    "VATEX-SA-34-2": ("international transport of passengers", "النقل الدولي للركاب"),
    "VATEX-SA-34-3": ("services directly connected and incidental to a Supply of international passenger transport",
                      None),  # Arabic differs from the source only in tanween placement (row 1b)
    "VATEX-SA-34-4": ("Supply of a qualifying means of transport", "توريد وسائل النقل المؤهلة"),
    "VATEX-SA-34-5": ("Any services relating to Goods or passenger transportation, as defined in article twenty five "
                      "of these Regulations",
                      "الخدمات ذات الصلة بنقل السلع أو الركاب، وفقاً للتعريف الوارد بالمادة الخامسة والعشرين من "
                      "اللائحة التنفيذية لنظام ضريبة القيامة المضافة"),  # "القيامة" as in the source
    "VATEX-SA-35": ("Medicines and medical equipment", "الأدوية والمعدات الطبية"),
    "VATEX-SA-36": ("Qualifying metals", "المعادن المؤهلة"),
    "VATEX-SA-EDU": ("Private education to citizen", "الخدمات التعليمية الخاصة للمواطنين"),
    "VATEX-SA-HEA": ("Private healthcare to citizen", "الخدمات الصحية الخاصة للمواطنين"),
    "VATEX-SA-MLTRY": ("supply of qualified military goods", "توريد السلع العسكرية المؤهلة"),
    "VATEX-SA-OOS": ("Reason is free text, to be provided by the taxpayer on case to case basis.",
                     "السبب يتم تزويده من قبل المكلف على أساس كل حالة على حدة"),
}

# Data Dictionary v1.2, KSA-10.
ZATCA_NOTE_REASONS = {
    "cancellation": ("Cancellation or suspension of the supplies after its occurrence either wholly or partially",
                     "تم إلغاء أو وقف التوريد بعد حدوثه أو اعتباره كلياً أو جزئياً"),
    "essential_change": ("In case of essential change or amendment in the supply, which leads to the change of the "
                         "VAT due", "وجود تغيير أو تعديل جوهري في طبيعة التوريد بحيث يؤدي الى تغيير الضريبة المستحقة"),
    "value_amendment": ("Amendment of the supply value which is pre-agreed upon between the supplier and consumer",
                        "تم الاتفاق على تعديل قيمة التوريد مسبقاً"),
    "refund": ("In case of goods or services refund.", "عند ترجيع السلع أو الخدمات"),
    "party_change": ("In case of change in Seller's or Buyer's information", "عند التعديل على بيانات المورد أو المشتري"),
}


# --- C1 (rows 1b, 1c, 2) and C7 (row 10c): reference data V2 -------------------------------------------

def test_c1_vatex_texts_are_zatcas_exact_texts():
    stored = {r.code: (r.name_en, r.name_ar) for r in VatExemptionReason.objects.all()}
    assert set(stored) == set(ZATCA_VATEX)
    for code, (en, ar) in ZATCA_VATEX.items():
        assert stored[code][0] == en, code
        if ar is not None:
            assert stored[code][1] == ar, code


def test_c1_note_reasons_are_the_data_dictionary_texts():
    assert {r.code: (r.name_en, r.name_ar) for r in NoteReason.objects.all()} == ZATCA_NOTE_REASONS


def test_c7_residential_rent_defaults_to_vatex_sa_30(w):
    assert IncomeType.objects.get(code="rent_residential").default_exemption_reason.code == "VATEX-SA-30"
    doc = final(w, invoice(w, [("rent_residential", "1", "1000.00")]))  # no reason picked: the default applies
    line = doc.lines.get()
    assert (line.vat_category, line.vat_exemption_reason.code, doc.total_vat) == ("E", "VATEX-SA-30", D("0.00"))


# --- C2 (row 1c): VATEX-SA-OOS carries the taxpayer's own reason ------------------------------------

def _draft(w, **fields):
    fields.setdefault("supply_date", date(2027, 1, 10))
    return services.create_sales_document(w.alm, user=w.clerk, customer=fields.pop("customer", w.customer),
                                          issue_date=date(2027, 1, 10), **fields)


def test_c2_out_of_scope_line_needs_its_reason_text(w):
    doc = _draft(w)
    oos = dict(income_type="other", income_account="4100", quantity="1", unit_price="200", description_ar="تعويض",
               vat_category="O", vat_exemption_reason="VATEX-SA-OOS")
    with pytest.raises(InvoicingError) as exc:
        services.add_sales_line(doc, user=w.clerk, **oos)
    assert "VATEX-SA-OOS" in str(exc.value.messages)
    with pytest.raises(InvoicingError):  # English only is not enough: the text is visible, so Arabic
        services.add_sales_line(doc, user=w.clerk, exemption_text_en="Compensation", **oos)
    with pytest.raises(InvoicingError):  # a text belongs only to VATEX-SA-OOS
        services.add_sales_line(doc, user=w.clerk, income_type="rent_residential", quantity="1", unit_price="10",
                                description_ar="إيجار", exemption_text_ar="سبب")
    services.add_sales_line(doc, user=w.clerk, exemption_text_ar="تعويض عن ضرر خارج نطاق الضريبة",
                            exemption_text_en="Damages, outside the scope of VAT", **oos)
    done = final(w, services.submit_sales_document(doc, user=w.clerk))
    group = next(g for g in done.vat_breakdown if g["category"] == "O")
    assert group["exemption_texts"] == [{"en": "Damages, outside the scope of VAT", "ar": "تعويض عن ضرر خارج نطاق الضريبة"}]
    assert done.lines.get().exemption_text_ar == "تعويض عن ضرر خارج نطاق الضريبة"


# --- C3 (row 1d): VATEX-SA-EDU / HEA keep and require the buyer's national ID -----------------------------

def test_c3_education_to_a_citizen_on_a_simplified_invoice_keeps_the_national_id(w):
    person = services.create_customer(w.alm, user=w.clerk, code="P1", name_ar="طالب", customer_kind="individual",
                                      **ADDRESS)
    services.add_customer_identifier(person, "IQA", "2123456789", user=w.clerk)
    edu = [("other", "1", "500", {"income_account": "4100", "vat_category": "Z", "vat_exemption_reason": "VATEX-SA-EDU"})]
    with pytest.raises(InvoicingError) as exc:  # no NAT (an Iqama is not enough)
        invoice(w, edu, customer=person)
    assert "national ID" in str(exc.value.messages)
    services.add_customer_identifier(person, "NAT", "1123456789", user=w.clerk)
    done = final(w, invoice(w, edu, customer=person))
    assert done.zatca_profile == "simplified"
    assert (done.buyer_other_id_scheme, done.buyer_other_id, done.buyer_name_ar) == ("NAT", "1123456789", "طالب")
    plain = final(w, invoice(w, [("rent_commercial", "1", "100")], customer=person))
    assert (plain.buyer_other_id_scheme, plain.buyer_other_id) == ("", "")  # R50 otherwise


def test_c3_healthcare_on_a_standard_invoice_needs_nat_even_with_a_vat_number(w):
    hea = [("other", "1", "500", {"income_account": "4100", "vat_category": "Z", "vat_exemption_reason": "VATEX-SA-HEA"})]
    with pytest.raises(InvoicingError) as exc:
        invoice(w, hea)  # a VAT-registered business without a national ID
    assert "national ID" in str(exc.value.messages)


# --- C4 (row 4b): export to a VAT-registered buyer --------------------------------------------------------

def test_c4_export_to_a_vat_registered_buyer_selects_another_buyer_id(w):
    lines = [("rent_commercial", "1", "100", {"vat_category": "Z", "vat_exemption_reason": "VATEX-SA-33"})]
    with pytest.raises(InvoicingError) as exc:
        invoice(w, lines, flag_export=True)
    assert "Buyer ID" in str(exc.value.messages)
    services.add_customer_identifier(w.customer, "TIN", "3999999999", user=w.clerk)
    done = final(w, invoice(w, lines, flag_export=True))
    assert (done.buyer_vat_number, done.buyer_other_id_scheme) == ("", "TIN")
    normal = final(w, invoice(w))  # not an export: the VAT number replaces the ID (BR-KSA-14)
    assert (normal.buyer_vat_number, normal.buyer_other_id_scheme) == ("310000000000003", "")


# --- C5 (row 8): sole traders and notes under the SAR 1,000 rule ---------------------------------------

def test_c5_vat_registered_individual_is_a_business_for_the_simplified_threshold(w):
    trader = services.create_customer(w.alm, user=w.clerk, code="T1", name_ar="تاجر فرد", customer_kind="individual",
                                      is_vat_registered=True, vat_number="311111111111113", **ADDRESS)
    assert _draft(w, customer=trader).zatca_profile == "standard"  # not simplified by default
    with pytest.raises(InvoicingError) as exc:
        approve(w, invoice(w, [("rent_commercial", "1", "869.57")], customer=trader, zatca_profile="simplified"))
    assert exc.value.code == "simplified_threshold"  # gross 1,000.01
    assert approve(w, invoice(w, [("rent_commercial", "1", "869.56")], customer=trader,
                              zatca_profile="simplified")).status == "issued"  # gross 999.99


def test_c5_note_of_1000_or_more_on_a_simplified_invoice_to_a_business_is_issuable(w):
    original = final(w, invoice(w, [("rent_commercial", "1", "869.56")], zatca_profile="simplified"))  # 999.99
    debit = final(w, invoice(w, [("rent_commercial", "1", "2000.00")], doc_type="debit_note",
                             original_document=original, payment_means_code="30", reason_ar="تعديل القيمة"))
    assert (debit.zatca_profile, debit.status, debit.total_gross) == ("simplified", "final", D("2300.00"))


# --- C6 (row 6f): late standard invoice warning --------------------------------------------------------

def test_c6_standard_invoice_after_the_15th_of_the_next_month_warns_but_issues(w):
    from invoicing.test_review_fixes import approve_on

    on_time = approve_on(w, invoice(w, supply_date=date(2027, 1, 10)), date(2027, 2, 15))
    late = approve_on(w, invoice(w, supply_date=date(2027, 1, 10)), date(2027, 2, 16))
    period = approve_on(w, invoice(w, supply_date=date(2027, 1, 1), supply_end_date=date(2027, 2, 5)),
                        date(2027, 2, 20))  # the supply ended in February
    assert not any("15 days" in m for m in on_time.warnings + period.warnings)
    assert late.status == "issued" and any("15 days" in m and "15 يومًا" in m for m in late.warnings)
    simplified = approve_on(w, invoice(w, [("rent_commercial", "1", "100")], supply_date=date(2027, 1, 10),
                                       zatca_profile="simplified"), date(2027, 2, 20))
    assert not any("15 days" in m for m in simplified.warnings)  # row 6f is about standard tax invoices
