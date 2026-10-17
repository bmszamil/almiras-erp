"""Statutory and spec reference data for invoicing (3.2), applied by migration ``invoicing.0002``.

Global data, no entity. Rates are data with effective dates, never constants. To change any of it,
add a new data migration; never edit V1 after it has been applied anywhere.

[compliance-checker to verify] VATEX codes and texts and the five note reasons are taken from the
ZATCA XML Implementation Standard / Data Dictionary v1.2 lists (KSA-10, BT-121) as understood by the
builder; the Arabic texts are marked [AR-review]. Income-type defaults (account, VAT category) are
prefills from invoicing.md D16 / §5 / OQ-39 and stay pending accountant and tax-adviser review.
"""

# category, rate, valid_from, valid_to, name_en, name_ar
VAT_RATES_V1 = (
    ("S", "5.00", "2018-01-01", "2020-06-30", "Standard rate 5%", "النسبة الأساسية 5%"),
    ("S", "15.00", "2020-07-01", None, "Standard rate 15%", "النسبة الأساسية 15%"),
    ("Z", "0.00", "2018-01-01", None, "Zero rate", "النسبة الصفرية"),
    ("E", "0.00", "2018-01-01", None, "Exempt", "معفى"),
    ("O", "0.00", "2018-01-01", None, "Out of scope", "خارج نطاق الضريبة"),
)

# code, category, name_en, name_ar [AR-review]
VATEX_V1 = (
    ("VATEX-SA-29", "E", "Financial services mentioned in Article 29 of the VAT Regulations", "الخدمات المالية"),
    ("VATEX-SA-29-7", "E", "Life insurance services mentioned in Article 29 of the VAT Regulations",
     "عقد تأمين على الحياة"),
    ("VATEX-SA-30", "E", "Real estate transactions mentioned in Article 30 of the VAT Regulations",
     "التوريدات العقارية المعفاة من الضريبة"),
    ("VATEX-SA-32", "Z", "Export of goods", "صادرات السلع من المملكة"),
    ("VATEX-SA-33", "Z", "Export of services", "صادرات الخدمات من المملكة"),
    ("VATEX-SA-34-1", "Z", "The international transport of goods", "النقل الدولي للسلع"),
    ("VATEX-SA-34-2", "Z", "International transport of passengers", "النقل الدولي للركاب"),
    ("VATEX-SA-34-3", "Z", "Services directly connected and incidental to a supply of international passenger transport",
     "الخدمات المرتبطة مباشرة أو عرضيًا بتوريد النقل الدولي للركاب"),
    ("VATEX-SA-34-4", "Z", "Supply of a qualifying means of transport", "توريد وسائل النقل المؤهلة"),
    ("VATEX-SA-34-5", "Z", "Any services relating to goods or passenger transportation (Article 25 of the Regulations)",
     "الخدمات ذات الصلة بنقل السلع أو الركاب وفقًا للمادة الخامسة والعشرين من اللائحة"),
    ("VATEX-SA-35", "Z", "Medicines and medical equipment", "الأدوية والمعدات الطبية"),
    ("VATEX-SA-36", "Z", "Qualifying metals", "المعادن المؤهلة"),
    ("VATEX-SA-EDU", "Z", "Private education to citizen", "الخدمات التعليمية الخاصة للمواطنين"),
    ("VATEX-SA-HEA", "Z", "Private healthcare to citizen", "الخدمات الصحية الخاصة للمواطنين"),
    ("VATEX-SA-MLTRY", "Z", "Supply of qualified military goods", "توريد السلع العسكرية المؤهلة"),
    ("VATEX-SA-OOS", "O", "Out of scope: reason given by the taxpayer case by case",
     "خارج النطاق: يحدد المكلف السبب لكل حالة"),
)

# code, name_en, name_ar [AR-review] — VAT IR Art. 40(1) / 54(3), DD v1.2 KSA-10
NOTE_REASONS_V1 = (
    ("cancellation", "Cancellation or suspension of the supplies after its occurrence either wholly or partially",
     "في حالة إلغاء أو إيقاف التوريد بعد حدوثه أو اعتباره كليًا أو جزئيًا"),
    ("essential_change", "In case of essential change or amendment in the supply, which leads to the change of the VAT due",
     "في حال وجود تغيير أو تعديل جوهري في التوريد بحيث يؤدي إلى تغيير الضريبة المستحقة"),
    ("value_amendment", "Amendment of the supply value which is pre-agreed upon between the supplier and consumer",
     "تعديل قيمة التوريد المتفق عليها مسبقًا بين المورد والمستهلك"),
    ("refund", "In case of goods or services refund", "في حالة استرجاع السلع أو الخدمات"),
    ("party_change", "In case of change in seller's or buyer's information",
     "في حالة التغيير في بيانات المورد أو المشتري"),
)

# code, name_en, name_ar, vat category, art51 class, account, intercompany account, real-estate rent
INCOME_TYPES_V1 = (
    ("rent_commercial", "Commercial rent", "إيجار تجاري", "S", "taxable", "4100", "4110", True),
    ("rent_residential", "Residential rent (VAT-exempt)", "إيجار سكني (معفى)", "E", "exempt", "4120", "4110", True),
    ("management_fee", "Management fees", "رسوم الإدارة", "S", "taxable", "4220", "4210", False),
    ("service_charge", "Service charges", "رسوم الخدمات المشتركة", "S", "taxable", "4230", "4210", False),
    ("service_fee", "Service fees", "أتعاب الخدمات", "S", "taxable", "4240", "4210", False),
    ("secondment", "Seconded staff", "إعارة الموظفين", "S", "taxable", "4200", "4210", False),
    ("car_rental", "Car rental", "تأجير السيارات", "S", "taxable", "4100", "4110", False),
    ("ip_licence", "IP and licence fees", "رسوم الملكية الفكرية والتراخيص", "S", "taxable", "4300", "4310", False),
    ("other", "Other income", "إيرادات أخرى", "S", "taxable", "", "", False),
)


def apply(apps):
    VatRate = apps.get_model("invoicing", "VatRate")
    Reason = apps.get_model("invoicing", "VatExemptionReason")
    NoteReason = apps.get_model("invoicing", "NoteReason")
    IncomeType = apps.get_model("invoicing", "IncomeType")
    for category, rate, start, end, en, ar in VAT_RATES_V1:
        VatRate.objects.update_or_create(category=category, valid_from=start,
                                         defaults=dict(rate=rate, valid_to=end, name_en=en, name_ar=ar))
    for code, category, en, ar in VATEX_V1:
        Reason.objects.update_or_create(code=code, defaults=dict(category=category, name_en=en, name_ar=ar))
    for code, en, ar in NOTE_REASONS_V1:
        NoteReason.objects.update_or_create(code=code, defaults=dict(name_en=en, name_ar=ar))
    for code, en, ar, cat, art51, account, ic, rent in INCOME_TYPES_V1:
        IncomeType.objects.update_or_create(code=code, defaults=dict(
            name_en=en, name_ar=ar, default_vat_category=cat, art51_class=art51, default_account_code=account,
            default_intercompany_account_code=ic, is_real_estate_rent=rent))
