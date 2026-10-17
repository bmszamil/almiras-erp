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
