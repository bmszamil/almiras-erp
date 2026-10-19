"""Bilingual errors and messages of the zatca app (R-33: every status, error and label has en/ar).

``ZatcaError`` carries both languages ("English / العربية") like ``invoicing.models.InvoicingError``.
ZATCA's own validation texts are stored as received (English); their Arabic comes from the maintained
code table below, else an "untranslated" label is shown (OQ-16 default).
"""
from django.core.exceptions import ValidationError


class ZatcaError(ValidationError):
    """A broken zatca rule; the message carries English and Arabic."""

    def __init__(self, en, ar, code=None, problems=None):
        self.en, self.ar = en, ar
        self.problems = list(problems or [])  # [(code, en, ar), ...] for local validation
        super().__init__(f"{en} / {ar}", code=code)
        self.code = code


def error(en, ar, code=None):
    return ZatcaError(en, ar, code=code)


UNTRANSLATED_AR = "رسالة من الهيئة غير مترجمة"

# Arabic for ZATCA validation codes (OQ-16: maintained table; extend as codes are met in the sandbox).
ZATCA_CODES_AR = {
    "BR-KSA-17": "سبب إصدار الإشعار الدائن أو المدين مطلوب.",
    "BR-KSA-56": "يجب أن يشير الإشعار الدائن أو المدين إلى رقم الفاتورة الأصلية.",
    "BR-KSA-44": "الرقم الضريبي للمشتري غير صالح.",
    "BR-KSA-40": "الرقم الضريبي للبائع غير صالح.",
    "BR-KSA-26": "تجزئة الفاتورة السابقة غير صحيحة.",
    "BR-KSA-33": "عداد الفاتورة غير صحيح.",
    "BR-KSA-08": "يجب أن تحتوي الفاتورة على معرّف واحد للبائع.",
    "BR-CO-17": "مبلغ ضريبة الفئة لا يساوي المبلغ الخاضع للضريبة مضروبًا في النسبة.",
    "BR-CO-14": "إجمالي الضريبة لا يساوي مجموع ضرائب الفئات.",
    "invalid-signature": "التوقيع الرقمي غير صالح.",
    "invalid-invoice-hash": "تجزئة الفاتورة غير صحيحة.",
    # Codes met in the developer-portal sandbox, 2026-10-19 (docs/compliance/zatca-sandbox-2026-10-19.md §2 to §5).
    "BR-CUSTOM-VALIDATION-01": "يجب ألا يطابق الرقم الضريبي للبائع (أو لمجموعته الضريبية) الرقمَ الضريبي للمشتري.",
    "certificate-permissions": "لا يُسمح إلا باستخدام الرقم الضريبي الوارد في شهادة المصادقة.",
    "certificate-issuer-name": "اسم جهة إصدار الشهادة في التوقيع لا يطابق الشهادة.",
    "invalid-signing-certificate": "شهادة التوقيع غير صالحة أو ليست شهادة الوحدة المصادق بها.",
    "signed-properties-hashing": "تجزئة الخصائص الموقعة في التوقيع غير صحيحة.",
    "invoiceHash_QRCODE_INVALID": "تجزئة الفاتورة في رمز الاستجابة السريعة لا تطابق الفاتورة.",
    "invoiceTimeStamp_QRCODE_INVALID": "تاريخ ووقت الفاتورة في رمز الاستجابة السريعة لا يطابقان تاريخ ووقت إصدارها.",
    "timestamp_QRCODE_INVALID": "الطابع الزمني في رمز الاستجابة السريعة غير صالح.",
    "invoiceTotal_QRCODE_INVALID": "إجمالي الفاتورة في رمز الاستجابة السريعة لا يطابق المبلغ المستحق.",
    "sellerName_QRCODE_INVALID": "اسم البائع في رمز الاستجابة السريعة لا يطابق الفاتورة.",
    "INVOICE_SIGNATURE_VALUE_QRCODE_INVALID": "قيمة التوقيع في رمز الاستجابة السريعة لا تطابق توقيع الفاتورة.",
    "QRCODE_INVALID": "رمز الاستجابة السريعة غير صالح أو ينقصه أحد الوسوم.",
    "XML-INVOICE-ERROR": "تعذرت قراءة ملف الفاتورة بصيغة XML.",
    "Missing-OTP": "رمز التحقق مطلوب.",
    "Incorrect-ComplianceRequestId": "رقم طلب الامتثال غير صحيح؛ يجب أن يتكون من 13 رقمًا.",
}


def arabic_for(code):
    """Arabic text for a ZATCA code, or "" (displayed as the untranslated label)."""
    return ZATCA_CODES_AR.get(code or "", "")


# Local messages: code -> (English, Arabic). Every refusal of the module uses one of these.
LOCAL = {
    "vat_number": ("The seller VAT number is missing or not 15 digits starting and ending with 3 (BR-KSA-40).",
                   "الرقم الضريبي للبائع ناقص أو لا يتكون من 15 رقمًا يبدأ وينتهي بالرقم 3."),
    "street": ("The seller street is missing (BR-KSA-09).", "شارع البائع ناقص."),
    "building_no": ("The seller building number must be exactly 4 digits (BR-KSA-37).",
                    "يجب أن يتكون رقم مبنى البائع من 4 أرقام بالضبط."),
    "postal_code": ("The seller postal code must be exactly 5 digits (BR-KSA-66).",
                    "يجب أن يتكون الرمز البريدي للبائع من 5 أرقام بالضبط."),
    "city": ("The seller city is missing (BR-KSA-09).", "مدينة البائع ناقصة."),
    "district": ("The seller district is missing (BR-KSA-09).", "حي البائع ناقص."),
    "country": ("The seller country code is missing (BR-KSA-09).", "رمز دولة البائع ناقص."),
    "additional_no": ("The seller additional number, when given, must be 4 digits.",
                      "الرقم الإضافي للبائع، إن وُجد، يجب أن يتكون من 4 أرقام."),
    "seller_name_ar": ("The seller legal name in Arabic is missing (R-33).", "الاسم القانوني للبائع بالعربية ناقص."),
    "seller_id": ("The seller has no identification (CRN, MOM, MLS, 700, SAG or OTH) for the XML (BR-KSA-08).",
                  "ليس للبائع معرّف (سجل تجاري أو رقم موحد أو غيرهما) يوضع في الفاتورة الإلكترونية."),
    "subtype": ("The invoice subtype must be 7 characters 01/02 followed by five 0/1 flags (BR-KSA-06).",
                "يجب أن يتكون نوع الفاتورة الفرعي من 7 خانات: 01 أو 02 ثم خمس علامات 0 أو 1."),
    "self_billed_export": ("A self-billed invoice cannot be an export invoice (BR-KSA-07).",
                           "لا تكون فاتورة الفوترة الذاتية فاتورة تصدير."),
    "simplified_flags": ("A simplified invoice allows only the third-party, nominal and summary flags (BR-KSA-31).",
                         "تسمح الفاتورة المبسطة فقط بعلامات الطرف الثالث والتوريد الاسمي والملخصة."),
    "export_buyer_vat": ("An export invoice carries no buyer VAT number (BR-KSA-46).",
                         "لا تحمل فاتورة التصدير رقمًا ضريبيًا للمشتري."),
    "buyer_vat": ("The buyer VAT number must be 15 digits starting and ending with 3 (BR-KSA-44).",
                  "يجب أن يتكون الرقم الضريبي للمشتري من 15 رقمًا يبدأ وينتهي بالرقم 3."),
    "buyer_missing": ("A standard invoice needs the buyer's VAT number or another buyer ID (BR-KSA-81).",
                      "تحتاج الفاتورة القياسية إلى الرقم الضريبي للمشتري أو معرّف آخر له."),
    "buyer_name": ("A standard invoice needs the buyer's name (BR-KSA-42).", "تحتاج الفاتورة القياسية إلى اسم المشتري."),
    "buyer_address": ("A standard invoice needs the buyer's street, city and country (BR-KSA-10).",
                      "تحتاج الفاتورة القياسية إلى شارع المشتري ومدينته ودولته."),
    "buyer_address_sa": ("A standard invoice to a buyer in Saudi Arabia needs the buyer's building number, postal "
                         "code and district (BR-KSA-63).",
                         "تحتاج الفاتورة القياسية لمشترٍ في المملكة إلى رقم مبنى المشتري ورمزه البريدي والحي."),
    "buyer_postal_code": ("The postal code of a buyer in Saudi Arabia must be exactly 5 digits (BR-KSA-67).",
                          "يجب أن يتكون الرمز البريدي لمشترٍ في المملكة من 5 أرقام بالضبط."),
    "supply_date": ("A standard tax invoice needs the supply date (BR-KSA-15).",
                    "تحتاج الفاتورة الضريبية القياسية إلى تاريخ التوريد."),
    "billing_reference": ("A credit or debit note must reference an e-invoice of the same entity (BR-KSA-56, R-19).",
                          "يجب أن يشير الإشعار الدائن أو المدين إلى فاتورة إلكترونية للكيان نفسه."),
    "reason": ("A credit or debit note needs its reason in Arabic, 1 to 1,000 characters (BR-KSA-17, R-19).",
               "يحتاج الإشعار الدائن أو المدين إلى سببه بالعربية من حرف واحد إلى 1000 حرف."),
    "original_pre_go_live": ("This note corrects an invoice issued before the entity went live on ZATCA Phase 2; that "
                             "invoice has no e-invoice to reference, so the note is not sent to ZATCA. How such notes "
                             "are reported is an open question (zatca R-19); ask the tax adviser.",
                             "يصحح هذا الإشعار فاتورة صدرت قبل بدء الكيان المرحلة الثانية مع الهيئة؛ ليس لتلك الفاتورة "
                             "فاتورة إلكترونية يُشار إليها، لذلك لا يُرسل الإشعار إلى الهيئة. طريقة الإبلاغ عن هذه "
                             "الإشعارات مسألة مفتوحة؛ استشر المستشار الضريبي."),
    "payment_means": ("A credit or debit note needs a payment means code (BR-49, R-19).",
                      "يحتاج الإشعار الدائن أو المدين إلى رمز وسيلة الدفع."),
    "category_follows": ("A note has the category (standard or simplified) of its original invoice (R-19).",
                         "يتبع الإشعار فئة فاتورته الأصلية (قياسية أو مبسطة)."),
    "header_net": ("The header net amount differs from the sum of the line net amounts (R-31).",
                   "المبلغ الصافي في رأس الفاتورة لا يساوي مجموع صافي البنود."),
    "category_vat": ("A VAT category amount differs from round(taxable amount x rate / 100, 2) (R-31, BR-CO-17).",
                     "ضريبة إحدى الفئات لا تساوي المبلغ الخاضع مضروبًا في النسبة مقربًا إلى خانتين."),
    "total_vat": ("The VAT total differs from the sum of the category VAT amounts (R-31, BR-CO-14).",
                  "إجمالي الضريبة لا يساوي مجموع ضرائب الفئات."),
    "total_gross": ("The total including VAT differs from the net total plus the VAT total (BR-CO-15).",
                    "الإجمالي شاملًا الضريبة لا يساوي الصافي مضافًا إليه إجمالي الضريبة."),
    "line_total": ("A line amount including VAT differs from its net amount plus its VAT (R-31, BR-KSA-51).",
                   "مبلغ أحد البنود شاملًا الضريبة لا يساوي صافيه مضافًا إليه ضريبته."),
    "sar_vat": ("A document in a currency other than SAR needs its VAT total in SAR from invoicing (R-40).",
                "يحتاج المستند بعملة غير الريال إلى إجمالي الضريبة بالريال من الفوترة."),
    "third_decimal": ("Amounts with a third decimal other than 0 are not sent to ZATCA: ZATCA XML amounts carry 2 "
                      "decimals in every currency (R-51; SDK R3.4.8 rejects 3-decimal sums, BR-CO-14/-15).",
                      "لا تُرسل إلى الهيئة المبالغ التي خانتها العشرية الثالثة ليست صفرًا: مبالغ فواتير الهيئة بخانتين "
                      "عشريتين لكل العملات."),
    "exemption_reasons": ("One VAT category carries several exemption reasons; ZATCA allows one per category.",
                          "تحمل فئة ضريبية واحدة عدة أسباب إعفاء؛ تسمح الهيئة بسبب واحد لكل فئة."),
    "exemption_reason": ("A zero-rated, exempt or out-of-scope category needs its exemption reason code and text.",
                         "تحتاج الفئة الصفرية أو المعفاة أو خارج النطاق إلى سبب الإعفاء."),
    "movable_acquisition": ("A movable capital asset document needs its acquisition date (R-38).",
                            "يحتاج مستند الأصل الرأسمالي المنقول إلى تاريخ الاقتناء."),
    "type_code": ("Only invoices (388), credit notes (381) and debit notes (383) are built; prepayment invoices (386) "
                  "wait for the invoicing prepayment flow (R-39).",
                  "تُنشأ الفواتير والإشعارات الدائنة والمدينة فقط؛ فاتورة الدفعة المقدمة (386) تنتظر بناء الدفعات "
                  "المقدمة في الفوترة."),
    "prepayment_not_built": ("Documents that deduct prepayments are not built yet; they wait for the invoicing "
                             "prepayment flow (R-39).",
                             "لا تُنشأ بعد المستندات التي تخصم دفعات مقدمة؛ تنتظر بناء الدفعات المقدمة في الفوترة."),
    "prepayment_reference": ("A prepaid amount needs a deduction line for each prepayment invoice with its number, issue "
                             "date, issue time and type code (BR-KSA-73, R-39).",
                             "يحتاج المبلغ المدفوع مقدمًا إلى بند خصم لكل فاتورة دفعة مقدمة برقمها وتاريخ ووقت إصدارها "
                             "ورمز نوعها."),
    "prepayment_type": ("A prepayment deduction line must reference a prepayment invoice, type code 386 (BR-KSA-74).",
                        "يجب أن يشير بند خصم الدفعة المقدمة إلى فاتورة دفعة مقدمة برمز النوع 386."),
    "prepayment_vat_fields": ("A prepayment deduction line needs the prepaid taxable amount, VAT amount, VAT category "
                              "and rate (BR-KSA-75).",
                              "يحتاج بند خصم الدفعة المقدمة إلى المبلغ الخاضع المدفوع مقدمًا وضريبته وفئة الضريبة "
                              "ونسبتها."),
    "prepayment_vat": ("The prepaid VAT of a deduction line differs from round(prepaid taxable amount x rate / 100, 2) "
                       "(BR-KSA-79).",
                       "ضريبة الدفعة المقدمة في بند الخصم لا تساوي المبلغ الخاضع مضروبًا في النسبة مقربًا إلى خانتين."),
    "prepaid_amount": ("The prepaid amount differs from the sum of the prepaid taxable amounts and their VAT "
                       "(BR-KSA-80).",
                       "المبلغ المدفوع مقدمًا لا يساوي مجموع المبالغ الخاضعة المدفوعة مقدمًا وضرائبها."),
    "prepayment_line_zero": ("A prepayment deduction line must have price, allowance, charge, net amount, line VAT and "
                             "line amount with VAT of 0 (BR-KSA-82).",
                             "يجب أن تكون قيم السعر والخصم والرسوم والصافي والضريبة والإجمالي في بند خصم الدفعة "
                             "المقدمة صفرًا."),
    "xml_size": ("The signed XML is larger than ZATCA's 10 MB limit (R-16).",
                 "حجم الفاتورة الموقعة يتجاوز حد الهيئة البالغ 10 ميغابايت."),
    "seller_name_length": ("The seller name is longer than 1000 characters (BT-27, BR-KSA-F-06-C10).",
                           "اسم البائع أطول من 1000 حرف."),
    "qr_length": ("The QR code is longer than 1000 characters (R-37, BR-CL-KSA-14); e.g. a very long seller name.",
                  "رمز الاستجابة السريعة أطول من 1000 حرف (مثل اسم بائع طويل جدًا)."),
    "qr_tags": ("The QR code of a simplified document lacks one of tags 1 to 9 (R-15); nothing was signed.",
                "ينقص رمز الاستجابة السريعة للمستند المبسط أحد الوسوم من 1 إلى 9؛ لم يُوقّع شيء."),
    "xsd": ("The XML does not comply with the UBL 2.1 schema.", "الفاتورة لا تطابق مخطط UBL 2.1."),
    "not_live": ("This entity is not live on ZATCA Phase 2 yet (no go-live date, or a future one); the document is "
                 "not submitted (R-28).",
                 "لم يبدأ هذا الكيان المرحلة الثانية مع الهيئة بعد؛ لا يُرسل المستند."),
    "cleared_xml_missing": ("ZATCA answered HTTP 200 without a cleared XML that can be read; the document is not "
                            "issued or posted and needs attention (R-22).",
                            "ردت الهيئة بالرمز 200 دون فاتورة معتمدة يمكن قراءتها؛ لم يصدر المستند ولم يُرحَّل ويحتاج إلى "
                            "متابعة."),
    "cleared_xml_uuid": ("The cleared XML returned by ZATCA carries another UUID than the document sent; it is not "
                         "issued or posted and needs attention (R-22).",
                         "تحمل الفاتورة المعتمدة التي أعادتها الهيئة معرّفًا فريدًا غير معرّف المستند المرسل؛ لم يصدر ولم "
                         "يُرحَّل ويحتاج إلى متابعة."),
    "cleared_xml_hash": ("The invoice hash of the cleared XML returned by ZATCA differs from the hash of the document "
                         "sent; it is not issued or posted and needs attention (R-22).",
                         "تختلف تجزئة الفاتورة المعتمدة التي أعادتها الهيئة عن تجزئة المستند المرسل؛ لم يصدر ولم يُرحَّل "
                         "ويحتاج إلى متابعة."),
    # CSR input checks (SDK R3.4.8 / R4.0.0 CsrGenerationService; findings 2026-10-17 §7.3), before build_csr.
    "csr_chars": ("The {field_en} for the ZATCA certificate request contains {chars}; ZATCA refuses any of "
                  "! @ # $ % & * _ < in CN, OU, O, location and industry. Change it before onboarding.",
                  "يحتوي {field_ar} في طلب شهادة الهيئة على {chars}؛ ترفض الهيئة أيًا من الرموز ! @ # $ % & * _ < في "
                  "الاسم الشائع والفرع واسم المنشأة والموقع والنشاط. عدّله قبل التسجيل."),
    "serial_equals": ("The EGS serial number must not contain '=' (ZATCA: special characters ('=') are not allowed).",
                      "يجب ألا يحتوي الرقم التسلسلي لوحدة الفوترة على الرمز '='."),
    "vat_group_ou": ("The VAT number's 11th digit is 1 (VAT group): the branch name used as OU in the certificate "
                     "request must be the 10-digit TIN of the group member whose unit is onboarded.",
                     "الخانة الحادية عشرة من الرقم الضريبي 1 (مجموعة ضريبية): يجب أن يكون اسم الفرع المستخدم في طلب "
                     "الشهادة هو الرقم المميز المكون من 10 أرقام لعضو المجموعة الذي تُسجَّل وحدته."),
    # B2G under VAT IR Art. 20(5) (KSA-40, BT-7; R4.0.0 rules BR-KSA-91 / -92).
    "buyer_legal_form": ("On a standard document the buyer's company legal form (KSA-40) can only be GOV, for a "
                         "government buyer (BR-KSA-91).",
                         "في المستند القياسي لا يكون الشكل القانوني للمشتري إلا GOV لجهة حكومية."),
    "tax_point_date": ("A standard document to a government buyer (legal form GOV) needs the tax point date, i.e. the "
                       "payment order date (BT-7, BR-KSA-92; VAT IR Art. 20(5)).",
                       "يحتاج المستند القياسي لجهة حكومية إلى تاريخ نقطة الضريبة، وهو تاريخ أمر الدفع."),
    # Sandbox run 2026-10-19 §4, §5 I1: ZATCA rejects at clearance; in neither SDK's rules (R-56).
    "BR-CUSTOM-VALIDATION-01": ("The buyer VAT number (BT-48) is the seller's own VAT number (BT-31); ZATCA rejects "
                                "such a document (BR-CUSTOM-VALIDATION-01). Correct the buyer.",
                                "الرقم الضريبي للمشتري هو الرقم الضريبي للبائع نفسه؛ ترفض الهيئة هذا المستند. صحّح "
                                "بيانات المشتري."),
    "no_egs": ("No active EGS unit of this entity can sign this category of document.",
               "لا توجد وحدة فوترة نشطة لهذا الكيان توقّع هذه الفئة من المستندات."),
}


def local(code, **fmt):
    en, ar = LOCAL[code]
    return code, en.format(**fmt), ar.format(**fmt)
