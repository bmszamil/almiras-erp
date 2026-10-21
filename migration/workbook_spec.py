"""The fillable migration workbook as data (migration.md §11, W1): sheets, columns, dropdown lists and fake examples.

``migration/workbook.py`` builds the xlsx template from this module and reads filled copies with it. Change the
workbook here, then run ``manage.py migration_template`` and commit the regenerated
``docs/migration/almiras-migration-workbook.xlsx`` (a test compares the two).

Choice codes mirror the ERP models (``core.Entity``, ``ledger.Account``, ``invoicing.Customer`` / ``Vendor`` /
``WhtCategory``, ``banking.BankAccount`` / ``Bank``, ``hr.Employee``) and the fixed-assets spec (3.1, 3.3); a test
checks them against the models. Every example value is fake. Arabic texts are for review [AR-review].
"""
import importlib
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from core.currency_seed import CURRENCY_SEED_V1
from core.iso3166 import ISO_3166_ALPHA2
from invoicing.reference_seed import WHT_CATEGORIES_V1

D = Decimal
FIRST_DATA_ROW = 6  # rows 1-5: title, legend, English header, Arabic header, field keys
MAX_ROWS = 2000  # rows prepared with formats and dropdowns
KEY_ROW, EN_ROW, AR_ROW = 5, 3, 4

BANKS_V1 = importlib.import_module("banking.migrations.0002_seed_banks").BANKS_V1

# --- dropdown lists: key -> [(code, English, Arabic)] ---------------------------------------------------------------

CHOICES = {
    "yes_no": [("yes", "Yes", "نعم"), ("no", "No", "لا")],
    "account_type": [("asset", "Asset", "أصل"), ("liability", "Liability", "التزام"), ("equity", "Equity", "حقوق ملكية"),
                     ("income", "Income", "إيراد"), ("expense", "Expense", "مصروف")],
    "legacy_system": [("falcon", "Falcon", "فالكون"), ("other", "Other system", "نظام آخر")],
    "party_kind": [("business", "Business", "منشأة"), ("individual", "Individual", "فرد"),
                   ("government", "Government", "جهة حكومية")],
    "ar_document": [("invoice", "Tax invoice", "فاتورة ضريبية"), ("debit_note", "Debit note", "إشعار مدين"),
                    ("credit_note", "Credit note", "إشعار دائن"),
                    ("advance", "Advance received, not applied", "دفعة مقدمة مستلمة غير مخصصة")],
    "ap_document": [("bill", "Vendor bill", "فاتورة مورد"), ("vendor_credit_note", "Vendor credit note", "إشعار دائن من مورد"),
                    ("advance", "Advance paid, not applied", "دفعة مقدمة مدفوعة غير مخصصة")],
    "wht_category": [(code, en, ar) for code, en, ar, _rate, _prohibited in WHT_CATEGORIES_V1],
    "bank": [(code, en, ar) for code, (en, ar, *_rest) in BANKS_V1.items()] + [("other", "Other bank", "بنك آخر")],
    "sharia_basis": [("islamic", "Islamic (profit)", "إسلامي (أرباح)"), ("conventional", "Conventional (interest)",
                                                                          "تقليدي (فوائد)")],
    "statement_format": [("mt940", "SWIFT MT940", "سويفت MT940"), ("camt053", "ISO 20022 camt.053", "كشف camt.053"),
                         ("csv", "Bank CSV export", "ملف CSV من البنك")],
    "id_type": [("national_id", "National ID", "الهوية الوطنية"), ("iqama", "Iqama (residence permit)", "الإقامة"),
                ("gcc_id", "GCC ID", "هوية خليجية"), ("passport", "Passport", "جواز السفر")],
    "gender": [("male", "Male", "ذكر"), ("female", "Female", "أنثى")],
    "employment_basis": [("ksa_employee", "Employee under Saudi labour law", "موظف خاضع لنظام العمل السعودي"),
                         ("local_entity_employee", "Employee of a local group company", "موظف لدى شركة محلية في المجموعة"),
                         ("remuneration", "Cross-border remuneration", "مكافأة شهرية عبر الحدود")],
    "gosi_cohort": [("legacy_m33", "Legacy (SIL M/33)", "النظام السابق (م/33)"),
                    ("new_m273", "New law (SIL M/273)", "النظام الجديد (م/273)")],
    "asset_class": [("land", "Land", "أراضٍ"), ("building", "Building", "مبانٍ"),
                    ("ip_land", "Investment property: land", "عقار استثماري: أرض"),
                    ("ip_building", "Investment property: building", "عقار استثماري: مبنى"),
                    ("vehicle", "Vehicle", "سيارة"), ("furniture_office", "Furniture and office equipment", "أثاث ومعدات مكتبية"),
                    ("it_equipment", "IT equipment", "أجهزة حاسب وتقنية"), ("machinery", "Machinery", "آلات ومعدات"),
                    ("leasehold_improvement", "Leasehold improvements", "تحسينات على عقارات مستأجرة"),
                    ("cwip", "Capital work in progress", "مشروعات تحت التنفيذ"), ("other", "Other", "أخرى")],
    "asset_usage": [("own_use", "Own use", "استخدام ذاتي"),
                    ("rental_operating_lease", "Rented out (operating lease)", "مؤجر (إيجار تشغيلي)"),
                    ("investment_property", "Investment property", "عقار استثماري")],
    "depreciation_method": [("straight_line", "Straight line", "القسط الثابت"),
                            ("reducing_balance", "Reducing balance", "القسط المتناقص")],
    "checklist_status": [("not_started", "Not started", "لم يبدأ"), ("in_progress", "In progress", "قيد التنفيذ"),
                         ("done", "Done", "منجز")],
}
CURRENCIES = {code: minor for code, (_n, _en, _ar, minor, _s) in CURRENCY_SEED_V1.items()}
COUNTRIES = sorted(ISO_3166_ALPHA2)
NON_DEPRECIABLE_CLASSES = {"land", "ip_land", "cwip"}
INVESTMENT_PROPERTY_CLASSES = {"ip_land", "ip_building"}


def label(choice):
    """The dropdown text of a choice: ``English / العربية``."""
    _code, en, ar = choice
    return f"{en} / {ar}"


# --- columns and sheets ------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    key: str
    en: str
    ar: str
    kind: str  # entity, text, legacy_code, erp_code, sar, amount, date, int, choice, yesno, currency, country, vat,
    #            cr, iban, rate, period (a month, YYYY-MM)
    required: bool = False
    choices: str = ""  # key of CHOICES for kind "choice"
    personal: bool = False  # personal data (PDPL): marked [PD], values never repeated in messages
    width: int = 16
    max_len: int = 200

    @property
    def header_en(self):
        return f"{self.en}{' *' if self.required else ''}{' [PD]' if self.personal else ''}"

    @property
    def header_ar(self):
        return f"{self.ar}{' *' if self.required else ''}{' [PD]' if self.personal else ''}"


@dataclass(frozen=True)
class Sheet:
    key: str
    name: str  # Excel tab name (English; Arabic in the title row)
    title_ar: str
    intro_en: str
    intro_ar: str
    columns: tuple
    examples: tuple = ()
    unique: tuple = ()  # tuples of column keys that must be unique together
    required_sheet: bool = False

    def column(self, key):
        return next(c for c in self.columns if c.key == key)


def C(key, en, ar, kind="text", **kw):
    return Column(key, en, ar, kind, **kw)


ENTITY = C("entity", "Entity code", "رمز الكيان", "entity", required=True, width=12)
ADDRESS = (
    C("building_no", "Building number", "رقم المبنى", max_len=4, width=10),
    C("street_ar", "Street (Arabic)", "الشارع (بالعربية)", width=22),
    C("street_en", "Street (English)", "الشارع (بالإنجليزية)", width=22),
    C("district_ar", "District (Arabic)", "الحي (بالعربية)", width=18),
    C("district_en", "District (English)", "الحي (بالإنجليزية)", width=18),
    C("city_ar", "City (Arabic)", "المدينة (بالعربية)", width=16),
    C("city_en", "City (English)", "المدينة (بالإنجليزية)", width=16),
    C("postal_code", "Postal code", "الرمز البريدي", max_len=10, width=10),
    C("country", "Country (ISO)", "الدولة (رمز ISO)", "country", required=True, width=10),
)


def _with_address(columns, personal):
    """Address columns; for parties they are personal data when the party is an individual."""
    return tuple(Column(c.key, c.en, c.ar, c.kind, c.required, c.choices, personal and c.key != "country", c.width,
                        c.max_len) for c in columns)


def _fake_sa_iban(bank_code, account):
    """A Saudi IBAN with valid check digits built from zeros (fake, for the example rows only)."""
    bban = bank_code + account.rjust(18, "0")
    digits = "".join(str(int(ch, 36)) for ch in bban + "SA00")
    return f"SA{98 - int(digits) % 97:02d}{bban}"


EXAMPLE_IBAN_1 = _fake_sa_iban("80", "999901")
EXAMPLE_IBAN_2 = _fake_sa_iban("10", "999902")
EXAMPLE_IBAN_3 = _fake_sa_iban("20", "999903")
CUT = date(2026, 12, 31)

SHEETS = (
    Sheet(
        "entities", "Entities", "الكيانات وتاريخ التحويل",
        "One row per company to migrate. Fill this sheet first: its codes feed the entity dropdowns.",
        "صف لكل شركة يتم ترحيل بياناتها. ابدأ بهذه الورقة: رموزها تظهر في قوائم الكيانات في باقي الأوراق.",
        (
            C("entity_code", "Entity code (ERP)", "رمز الكيان في النظام", "text", required=True, width=12, max_len=8),
            C("name_en", "Name (English)", "الاسم (بالإنجليزية)", required=True, width=28),
            C("name_ar", "Name (Arabic)", "الاسم (بالعربية)", required=True, width=28),
            C("cr_number", "Commercial registration", "السجل التجاري", "cr", width=14),
            C("vat_number", "VAT number", "الرقم الضريبي", "vat", width=18),
            C("parent_entity", "Parent entity code", "رمز الكيان الأم", "entity", width=12),
            C("cutover_date", "Cut-over date", "تاريخ التحويل", "date", required=True, width=13),
            C("legacy_system", "Legacy system", "النظام السابق", "choice", required=True, choices="legacy_system"),
            C("legacy_company", "Company in the legacy system", "اسم أو رقم الشركة في النظام السابق", width=24),
            C("functional_currency", "Functional currency", "العملة الوظيفية", "currency", required=True, width=10),
            C("sharia_enforced", "Sharia rules enforced", "تطبيق الضوابط الشرعية", "yesno", required=True, width=12),
            C("pl_to_opening_equity", "Net income and expenses into opening equity", "ترحيل صافي الإيرادات والمصروفات إلى حقوق الملكية الافتتاحية",
              "yesno", required=True, width=16),
            C("replay_journals", "Replay the year's journals", "إعادة ترحيل قيود السنة", "yesno", required=True, width=12),
            C("notes", "Notes", "ملاحظات", width=30, max_len=500),
        ),
        examples=(
            {"entity_code": "EXA", "name_en": "Example Holding Co (fake)", "name_ar": "شركة مثال القابضة (وهمية)",
             "cr_number": "9999999901", "vat_number": "399999999900003", "parent_entity": None, "cutover_date": CUT,
             "legacy_system": "falcon", "legacy_company": "EXAMPLE-1", "functional_currency": "SAR",
             "sharia_enforced": "yes", "pl_to_opening_equity": "no", "replay_journals": "no",
             "notes": "Example row: delete / صف مثال: احذفه"},
            {"entity_code": "EXB", "name_en": "Example Services Co (fake)", "name_ar": "شركة مثال للخدمات (وهمية)",
             "cr_number": "9999999902", "vat_number": None, "parent_entity": "EXA", "cutover_date": CUT,
             "legacy_system": "other", "legacy_company": "EXAMPLE-2", "functional_currency": "SAR",
             "sharia_enforced": "yes", "pl_to_opening_equity": "no", "replay_journals": "no",
             "notes": "Example row: delete / صف مثال: احذفه"},
        ),
        unique=(("entity_code",),),
        required_sheet=True,
    ),
    Sheet(
        "falcon_chart", "Falcon chart", "دليل حسابات فالكون",
        "The legacy chart of accounts as exported: every account, header accounts with Postable = No.",
        "دليل الحسابات في النظام السابق كما هو: كل الحسابات، والحسابات الرئيسية بقيمة «قابل للترحيل = لا».",
        (
            ENTITY,
            C("code", "Falcon account code", "كود الحساب في فالكون", "legacy_code", required=True, width=12),
            C("name_ar", "Account name (Arabic)", "اسم الحساب (بالعربية)", required=True, width=30),
            C("name_en", "Account name (English)", "اسم الحساب (بالإنجليزية)", width=30),
            C("type", "Account type", "نوع الحساب", "choice", required=True, choices="account_type", width=14),
            C("parent", "Parent account code", "كود الحساب الرئيسي", "legacy_code", width=12),
            C("postable", "Postable", "قابل للترحيل", "yesno", required=True, width=10),
        ),
        examples=(
            {"entity": "EXA", "code": "9910", "name_ar": "نقدية (مثال)", "name_en": "Cash (example)", "type": "asset",
             "parent": None, "postable": "no"},
            {"entity": "EXA", "code": "99101", "name_ar": "بنك مثال", "name_en": "Example bank", "type": "asset",
             "parent": "9910", "postable": "yes"},
        ),
        unique=(("entity", "code"),),
    ),
    Sheet(
        "trial_balance", "Trial balance", "ميزان المراجعة في تاريخ التحويل",
        "Closing balance of every postable account on the entity's cut-over date, in SAR: debit or credit, not both.",
        "الرصيد الختامي لكل حساب قابل للترحيل في تاريخ التحويل للكيان، بالريال: مدين أو دائن وليس كليهما.",
        (
            ENTITY,
            C("code", "Falcon account code", "كود الحساب في فالكون", "legacy_code", required=True, width=12),
            C("name", "Account name (as in Falcon)", "اسم الحساب (كما في فالكون)", width=30),
            C("debit", "Debit (SAR)", "مدين (ريال)", "sar", width=16),
            C("credit", "Credit (SAR)", "دائن (ريال)", "sar", width=16),
        ),
        examples=(
            {"entity": "EXA", "code": "99101", "name": "بنك مثال", "debit": D("1000.00"), "credit": None},
            {"entity": "EXA", "code": "99201", "name": "رأس مال مثال", "debit": None, "credit": D("1000.00")},
        ),
        unique=(("entity", "code"),),
        required_sheet=True,
    ),
    Sheet(
        "account_map", "Account mapping", "ربط الحسابات",
        "Each Falcon account to one ERP account (see the ERP chart sheet). Intercompany ERP accounts need the "
        "counterparty entity. A second person approves the map in the ERP.",
        "اربط كل حساب في فالكون بحساب واحد في النظام (انظر ورقة دليل حسابات النظام). حسابات شركات المجموعة تحتاج "
        "إلى الكيان المقابل. يعتمد الربط شخص آخر في النظام.",
        (
            ENTITY,
            C("legacy_code", "Falcon account code", "كود الحساب في فالكون", "legacy_code", required=True, width=12),
            C("legacy_name", "Falcon account name", "اسم الحساب في فالكون", width=30),
            C("erp_code", "ERP account code", "رمز الحساب في النظام", "erp_code", required=True, width=12),
            C("counterparty", "Counterparty entity (intercompany)", "الكيان المقابل (شركات المجموعة)", "entity", width=14),
            C("note", "Notes", "ملاحظات", width=40, max_len=300),
        ),
        examples=(
            {"entity": "EXA", "legacy_code": "99101", "legacy_name": "بنك مثال", "erp_code": "1100",
             "counterparty": None, "note": "Example row: delete / صف مثال: احذفه"},
            {"entity": "EXA", "legacy_code": "99121", "legacy_name": "مستحق من شركة مثال", "erp_code": "1210",
             "counterparty": "EXB", "note": "Example row: delete / صف مثال: احذفه"},
        ),
        unique=(("entity", "legacy_code"),),
        required_sheet=True,
    ),
    Sheet(
        "customers", "Customers", "العملاء",
        "Every customer with an open item, and active customers. Names and addresses of individuals are personal data.",
        "كل عميل له رصيد مفتوح والعملاء النشطون. أسماء الأفراد وعناوينهم بيانات شخصية.",
        (
            ENTITY,
            C("code", "Customer code", "رمز العميل", required=True, width=12, max_len=20),
            C("name_ar", "Name (Arabic)", "الاسم (بالعربية)", required=True, width=28, personal=True),
            C("name_en", "Name (English)", "الاسم (بالإنجليزية)", width=28, personal=True),
            C("kind", "Individual or company", "فرد أو منشأة", "choice", required=True, choices="party_kind", width=14),
            C("vat_number", "VAT number", "الرقم الضريبي", "vat", width=18),
            C("cr_number", "Commercial registration", "السجل التجاري", "cr", width=14),
            *_with_address(ADDRESS, personal=True),
            C("currency", "Currency", "العملة", "currency", required=True, width=10),
            C("payment_terms_days", "Payment terms (days)", "مدة السداد (أيام)", "int", width=10),
            C("group_entity", "Group entity (intercompany)", "كيان من المجموعة", "entity", width=12),
        ),
        examples=(
            {"entity": "EXA", "code": "C-EX1", "name_ar": "عميل مثال للتجارة", "name_en": "Example Customer Trading",
             "kind": "business", "vat_number": "399999999900013", "cr_number": "9999999911", "building_no": "9999",
             "street_ar": "شارع المثال", "street_en": "Example St", "district_ar": "حي المثال", "district_en": "Example",
             "city_ar": "الرياض", "city_en": "Riyadh", "postal_code": "99999", "country": "SA", "currency": "SAR",
             "payment_terms_days": 30, "group_entity": None},
            {"entity": "EXA", "code": "C-EX2", "name_ar": "شركة مثال للخدمات", "name_en": "Example Services Co",
             "kind": "business", "vat_number": None, "cr_number": None, "building_no": None, "street_ar": None,
             "street_en": None, "district_ar": None, "district_en": None, "city_ar": None, "city_en": None,
             "postal_code": None, "country": "SA", "currency": "SAR", "payment_terms_days": 30, "group_entity": "EXB"},
        ),
        unique=(("entity", "code"),),
    ),
    Sheet(
        "vendors", "Vendors", "الموردون",
        "Every vendor with an open item, and active vendors. Non-residents need the withholding tax category.",
        "كل مورد له رصيد مفتوح والموردون النشطون. غير المقيمين يحتاجون إلى فئة ضريبة الاستقطاع.",
        (
            ENTITY,
            C("code", "Vendor code", "رمز المورد", required=True, width=12, max_len=20),
            C("name_ar", "Name (Arabic)", "الاسم (بالعربية)", required=True, width=28, personal=True),
            C("name_en", "Name (English)", "الاسم (بالإنجليزية)", width=28, personal=True),
            C("kind", "Individual or company", "فرد أو منشأة", "choice", required=True, choices="party_kind", width=14),
            C("vat_number", "VAT number", "الرقم الضريبي", "vat", width=18),
            C("cr_number", "Commercial registration", "السجل التجاري", "cr", width=14),
            *_with_address(ADDRESS, personal=True),
            C("currency", "Currency", "العملة", "currency", required=True, width=10),
            C("payment_terms_days", "Payment terms (days)", "مدة السداد (أيام)", "int", width=10),
            C("resident", "Resident in KSA", "مقيم في المملكة", "yesno", required=True, width=10),
            C("residence_country", "Country of residence (non-residents)", "دولة الإقامة (لغير المقيمين)", "country", width=12),
            C("wht_category", "Withholding tax category", "فئة ضريبة الاستقطاع", "choice", choices="wht_category", width=22),
            C("wht_gross_up", "Company bears the WHT (gross-up)", "تتحمل الشركة ضريبة الاستقطاع", "yesno", width=12),
            C("iban", "IBAN", "رقم الآيبان", "iban", width=28, personal=True, max_len=34),
            C("group_entity", "Group entity (intercompany)", "كيان من المجموعة", "entity", width=12),
        ),
        examples=(
            {"entity": "EXA", "code": "V-EX1", "name_ar": "مورد مثال للصيانة", "name_en": "Example Maintenance Supplier",
             "kind": "business", "vat_number": "399999999900023", "cr_number": "9999999921", "building_no": None,
             "street_ar": None, "street_en": None, "district_ar": None, "district_en": None, "city_ar": "جدة",
             "city_en": "Jeddah", "postal_code": None, "country": "SA", "currency": "SAR", "payment_terms_days": 45,
             "resident": "yes", "residence_country": None, "wht_category": None, "wht_gross_up": None,
             "iban": EXAMPLE_IBAN_3, "group_entity": None},
            {"entity": "EXA", "code": "V-EX2", "name_ar": "مستشار مثال الخارجي", "name_en": "Example Advisers Ltd",
             "kind": "business", "vat_number": None, "cr_number": None, "building_no": None, "street_ar": None,
             "street_en": None, "district_ar": None, "district_en": None, "city_ar": None, "city_en": "London",
             "postal_code": None, "country": "GB", "currency": "USD", "payment_terms_days": 30, "resident": "no",
             "residence_country": "GB", "wht_category": "technical_services", "wht_gross_up": "no", "iban": None,
             "group_entity": None},
        ),
        unique=(("entity", "code"),),
    ),
    Sheet(
        "open_receivables", "Open receivables", "الذمم المدينة المفتوحة",
        "Every customer document still unpaid on the cut-over date, with the amount still open. The SAR total "
        "must equal the receivables accounts in the trial balance.",
        "كل مستند عميل غير مسدد في تاريخ التحويل بالمبلغ المتبقي. يجب أن يساوي المجموع بالريال أرصدة حسابات "
        "العملاء في ميزان المراجعة.",
        (
            ENTITY,
            C("customer", "Customer code", "رمز العميل", required=True, width=12, max_len=20),
            C("document_type", "Document type", "نوع المستند", "choice", required=True, choices="ar_document", width=18),
            C("document_no", "Document number", "رقم المستند", required=True, width=16, max_len=40),
            C("document_date", "Document date", "تاريخ المستند", "date", required=True, width=13),
            C("due_date", "Due date", "تاريخ الاستحقاق", "date", width=13),
            C("currency", "Currency", "العملة", "currency", required=True, width=10),
            C("amount", "Open amount (document currency)", "المبلغ المتبقي (بعملة المستند)", "amount", required=True),
            C("sar_amount", "Open amount (SAR)", "المبلغ المتبقي (ريال)", "sar", required=True),
            C("description", "Description", "البيان", width=30, max_len=300),
        ),
        examples=(
            {"entity": "EXA", "customer": "C-EX1", "document_type": "invoice", "document_no": "EX-INV-0001",
             "document_date": date(2026, 11, 30), "due_date": date(2026, 12, 30), "currency": "SAR",
             "amount": D("11500.00"), "sar_amount": D("11500.00"), "description": "Example row: delete / صف مثال: احذفه"},
            {"entity": "EXA", "customer": "C-EX1", "document_type": "credit_note", "document_no": "EX-CN-0001",
             "document_date": date(2026, 12, 15), "due_date": None, "currency": "SAR", "amount": D("1150.00"),
             "sar_amount": D("1150.00"), "description": "Example row: delete / صف مثال: احذفه"},
        ),
        unique=(("entity", "customer", "document_type", "document_no"),),
    ),
    Sheet(
        "open_payables", "Open payables", "الذمم الدائنة المفتوحة",
        "Every vendor document still unpaid on the cut-over date, with the amount still open. The SAR total must "
        "equal the payables accounts in the trial balance.",
        "كل مستند مورد غير مسدد في تاريخ التحويل بالمبلغ المتبقي. يجب أن يساوي المجموع بالريال أرصدة حسابات "
        "الموردين في ميزان المراجعة.",
        (
            ENTITY,
            C("vendor", "Vendor code", "رمز المورد", required=True, width=12, max_len=20),
            C("document_type", "Document type", "نوع المستند", "choice", required=True, choices="ap_document", width=18),
            C("document_no", "Vendor document number", "رقم مستند المورد", required=True, width=16, max_len=40),
            C("document_date", "Document date", "تاريخ المستند", "date", required=True, width=13),
            C("due_date", "Due date", "تاريخ الاستحقاق", "date", width=13),
            C("currency", "Currency", "العملة", "currency", required=True, width=10),
            C("amount", "Open amount (document currency)", "المبلغ المتبقي (بعملة المستند)", "amount", required=True),
            C("sar_amount", "Open amount (SAR)", "المبلغ المتبقي (ريال)", "sar", required=True),
            C("description", "Description", "البيان", width=30, max_len=300),
        ),
        examples=(
            {"entity": "EXA", "vendor": "V-EX1", "document_type": "bill", "document_no": "EX-B-77",
             "document_date": date(2026, 12, 10), "due_date": date(2027, 1, 24), "currency": "SAR",
             "amount": D("2300.00"), "sar_amount": D("2300.00"), "description": "Example row: delete / صف مثال: احذفه"},
            {"entity": "EXA", "vendor": "V-EX2", "document_type": "bill", "document_no": "EX-UK-12",
             "document_date": date(2026, 12, 20), "due_date": date(2027, 1, 19), "currency": "USD",
             "amount": D("1000.00"), "sar_amount": D("3750.00"), "description": "Example row: delete / صف مثال: احذفه"},
        ),
        unique=(("entity", "vendor", "document_type", "document_no"),),
    ),
    Sheet(
        "bank_accounts", "Bank accounts", "الحسابات البنكية",
        "Every bank account. Book balance = the Falcon account's balance on the cut-over date; statement balance = "
        "the bank statement on that date (for the first reconciliation).",
        "كل حساب بنكي. الرصيد الدفتري = رصيد حساب فالكون في تاريخ التحويل؛ رصيد الكشف = رصيد كشف البنك في "
        "التاريخ نفسه (لأول تسوية بنكية).",
        (
            ENTITY,
            C("code", "Account code (short)", "رمز الحساب (مختصر)", required=True, width=14, max_len=30),
            C("name_ar", "Account name (Arabic)", "اسم الحساب (بالعربية)", required=True, width=24),
            C("name_en", "Account name (English)", "اسم الحساب (بالإنجليزية)", required=True, width=24),
            C("bank", "Bank", "البنك", "choice", required=True, choices="bank", width=22),
            C("bank_name_other", "Bank name (if other)", "اسم البنك (إن كان آخر)", width=20),
            C("iban", "IBAN", "رقم الآيبان", "iban", width=28, max_len=34),
            C("account_number", "Account number (no IBAN only)", "رقم الحساب (عند عدم وجود آيبان)", width=18, max_len=40),
            C("currency", "Currency", "العملة", "currency", required=True, width=10),
            C("falcon_code", "Falcon GL account code", "كود حساب الأستاذ في فالكون", "legacy_code", required=True, width=12),
            C("sharia_basis", "Sharia basis", "الأساس الشرعي", "choice", required=True, choices="sharia_basis", width=18),
            C("statement_format", "Statement format", "صيغة الكشف", "choice", choices="statement_format", width=16),
            C("book_balance", "Book balance at cut-over (account currency)", "الرصيد الدفتري في تاريخ التحويل (بعملة الحساب)",
              "amount", required=True),
            C("book_balance_sar", "Book balance at cut-over (SAR)", "الرصيد الدفتري في تاريخ التحويل (ريال)", "sar",
              required=True),
            C("statement_balance", "Bank statement balance at cut-over", "رصيد كشف البنك في تاريخ التحويل", "amount"),
            C("is_purification", "Purification account", "حساب التطهير", "yesno", width=10),
        ),
        examples=(
            {"entity": "EXA", "code": "EX-RJ-SAR", "name_ar": "حساب مثال جاري", "name_en": "Example current account",
             "bank": "alrajhi", "bank_name_other": None, "iban": EXAMPLE_IBAN_1, "account_number": None,
             "currency": "SAR", "falcon_code": "99101", "sharia_basis": "islamic", "statement_format": "mt940",
             "book_balance": D("1000.00"), "book_balance_sar": D("1000.00"), "statement_balance": D("1250.00"),
             "is_purification": "no"},
            {"entity": "EXA", "code": "EX-SNB-USD", "name_ar": "حساب مثال بالدولار", "name_en": "Example USD account",
             "bank": "snb", "bank_name_other": None, "iban": EXAMPLE_IBAN_2, "account_number": None,
             "currency": "USD", "falcon_code": "99102", "sharia_basis": "islamic", "statement_format": "camt053",
             "book_balance": D("100.00"), "book_balance_sar": D("375.00"), "statement_balance": D("100.00"),
             "is_purification": "no"},
        ),
        unique=(("entity", "code"),),
    ),
    Sheet(
        "employees", "Employees", "الموظفون",
        "Minimal HR data per employee on the cut-over date. Columns marked [PD] are personal data (PDPL): fill them "
        "only in the secured copy, never by e-mail.",
        "الحد الأدنى من بيانات الموارد البشرية لكل موظف في تاريخ التحويل. الأعمدة المعلمة [PD] بيانات شخصية "
        "(نظام حماية البيانات الشخصية): تُعبأ في النسخة المحمية فقط ولا تُرسل بالبريد.",
        (
            ENTITY,
            C("employee_no", "Employee number (legacy)", "الرقم الوظيفي (السابق)", required=True, width=12, max_len=20),
            C("full_name_ar", "Full name (Arabic)", "الاسم الكامل (بالعربية)", required=True, personal=True, width=28),
            C("full_name_en", "Full name (English)", "الاسم الكامل (بالإنجليزية)", required=True, personal=True, width=28),
            C("nationality", "Nationality (ISO)", "الجنسية (رمز ISO)", "country", required=True, width=10),
            C("id_type", "ID type", "نوع الهوية", "choice", required=True, choices="id_type", width=16),
            C("id_number", "ID number", "رقم الهوية", required=True, personal=True, width=14, max_len=20),
            C("date_of_birth", "Date of birth", "تاريخ الميلاد", "date", required=True, personal=True, width=13),
            C("gender", "Gender", "الجنس", "choice", required=True, choices="gender", personal=True, width=10),
            C("hire_date", "Hire date", "تاريخ التعيين", "date", required=True, width=13),
            C("employment_basis", "Employment basis", "أساس التوظيف", "choice", required=True, choices="employment_basis",
              width=22),
            C("job_title_ar", "Job title (Arabic)", "المسمى الوظيفي (بالعربية)", required=True, width=22),
            C("job_title_en", "Job title (English)", "المسمى الوظيفي (بالإنجليزية)", required=True, width=22),
            C("department", "Department", "الإدارة", width=18),
            C("basic_salary", "Basic salary (SAR, monthly)", "الراتب الأساسي (ريال شهريًا)", "sar", required=True, personal=True),
            C("housing_allowance", "Housing allowance (SAR, monthly)", "بدل السكن (ريال شهريًا)", "sar", personal=True),
            C("transport_allowance", "Transport allowance (SAR, monthly)", "بدل النقل (ريال شهريًا)", "sar", personal=True),
            C("other_allowances", "Other allowances (SAR, monthly)", "بدلات أخرى (ريال شهريًا)", "sar", personal=True),
            C("gosi_number", "GOSI number", "رقم التأمينات", personal=True, width=14, max_len=20),
            C("gosi_cohort", "GOSI cohort (Saudis)", "فئة التأمينات (للسعوديين)", "choice", choices="gosi_cohort",
              personal=True, width=18),
            C("salary_bank", "Salary bank", "بنك الراتب", "choice", choices="bank", personal=True, width=20),
            C("iban", "Salary IBAN", "آيبان الراتب", "iban", personal=True, width=28, max_len=34),
            C("eosb_provision", "End-of-service provision at cut-over (SAR)", "مخصص نهاية الخدمة في تاريخ التحويل (ريال)",
              "sar", personal=True),
            C("leave_balance_days", "Annual leave balance (days)", "رصيد الإجازة السنوية (أيام)", "rate", width=10),
        ),
        examples=(
            {"entity": "EXA", "employee_no": "EX-001", "full_name_ar": "موظف مثال الأول", "full_name_en": "Example Employee One",
             "nationality": "SA", "id_type": "national_id", "id_number": "1999999901", "date_of_birth": date(1990, 1, 1),
             "gender": "male", "hire_date": date(2020, 1, 1), "employment_basis": "ksa_employee",
             "job_title_ar": "محاسب", "job_title_en": "Accountant", "department": "Finance", "basic_salary": D("10000.00"),
             "housing_allowance": D("2500.00"), "transport_allowance": D("1000.00"), "other_allowances": None,
             "gosi_number": "999999901", "gosi_cohort": "legacy_m33", "salary_bank": "alrajhi", "iban": EXAMPLE_IBAN_1,
             "eosb_provision": D("35000.00"), "leave_balance_days": D("12.5")},
            {"entity": "EXA", "employee_no": "EX-002", "full_name_ar": "موظفة مثال الثانية",
             "full_name_en": "Example Employee Two", "nationality": "EG", "id_type": "iqama", "id_number": "2999999902",
             "date_of_birth": date(1992, 6, 1), "gender": "female", "hire_date": date(2023, 3, 1),
             "employment_basis": "ksa_employee", "job_title_ar": "مساعدة إدارية", "job_title_en": "Administrative assistant",
             "department": "Admin", "basic_salary": D("6000.00"), "housing_allowance": D("1500.00"),
             "transport_allowance": D("500.00"), "other_allowances": None, "gosi_number": None, "gosi_cohort": None,
             "salary_bank": "snb", "iban": EXAMPLE_IBAN_2, "eosb_provision": D("9000.00"), "leave_balance_days": D("21")},
        ),
        unique=(("entity", "employee_no"),),
    ),
    Sheet(
        # migration W14 (dry run 2026-10-21 gap 4): the bai' ajel contracts behind 1240 / 1241, loaded into payroll
        # and invoicing as migrated contracts so payroll collects their remaining instalments.
        "bai_ajel_contracts", "Bai' ajel contracts", "عقود البيع الآجل",
        "Every employee bai' ajel (deferred-payment sale) contract with a balance on the cut-over date. The amounts "
        "still open must equal the employee deferred-sale receivable and the deferred profit in the trial balance.",
        "كل عقد بيع آجل لموظف عليه رصيد في تاريخ التحويل. يجب أن تساوي المبالغ المتبقية ذمم البيع الآجل للموظفين "
        "والأرباح المؤجلة في ميزان المراجعة.",
        (
            ENTITY,
            C("contract_no", "Contract number (legacy)", "رقم العقد (السابق)", required=True, width=14, max_len=40),
            C("employee_no", "Employee number (as on the Employees sheet)", "الرقم الوظيفي (كما في ورقة الموظفين)",
              required=True, width=14, max_len=20),
            C("asset_description_ar", "Asset sold (Arabic)", "الأصل المبيع (بالعربية)", required=True, width=26),
            C("asset_description_en", "Asset sold (English)", "الأصل المبيع (بالإنجليزية)", width=26),
            C("sale_date", "Sale date", "تاريخ البيع", "date", required=True, width=13),
            C("sale_price", "Sale price (cost + profit, SAR, before VAT)",
              "ثمن البيع (التكلفة + الربح، ريال، قبل الضريبة)", "sar", required=True),
            C("profit_amount", "Fixed profit in the price (SAR)", "الربح الثابت ضمن الثمن (ريال)", "sar", required=True),
            C("vat_amount", "VAT charged to the employee (SAR)", "الضريبة المحملة على الموظف (ريال)", "sar"),
            C("instalments", "Number of instalments (whole contract)", "عدد الأقساط (للعقد كله)", "int", required=True,
              width=10),
            C("paid_to_date", "Paid up to the cut-over (SAR, of price + VAT)",
              "المسدد حتى تاريخ التحويل (ريال، من الثمن والضريبة)", "sar", required=True),
            C("next_instalment_period", "First instalment after the cut-over (YYYY-MM)",
              "أول قسط بعد تاريخ التحويل (سنة-شهر)", "period", required=True, width=12, max_len=7),
            C("consent_ref", "Signed salary-deduction consent (reference)", "مرجع الموافقة الموقعة على الاستقطاع",
              width=22, max_len=200),
        ),
        examples=(
            {"entity": "EXA", "contract_no": "EX-BAJ-01", "employee_no": "EX-001", "asset_description_ar": "سيارة مثال",
             "asset_description_en": "Example car", "sale_date": date(2025, 1, 15), "sale_price": D("60000.00"),
             "profit_amount": D("6000.00"), "vat_amount": D("9000.00"), "instalments": 36,
             "paid_to_date": D("44083.33"), "next_instalment_period": "2027-01", "consent_ref": "EX-CONSENT-01"},
            {"entity": "EXA", "contract_no": "EX-BAJ-02", "employee_no": "EX-002", "asset_description_ar": "أثاث مثال",
             "asset_description_en": "Example furniture", "sale_date": date(2026, 6, 1), "sale_price": D("12000.00"),
             "profit_amount": D("1000.00"), "vat_amount": D("1800.00"), "instalments": 12,
             "paid_to_date": D("6900.00"), "next_instalment_period": "2027-01", "consent_ref": None},
        ),
        unique=(("entity", "contract_no"),),
    ),
    Sheet(
        "fixed_assets", "Fixed assets", "الأصول الثابتة",
        "The asset register on the cut-over date, one row per asset (or per category where no detail exists). Cost "
        "and accumulated depreciation must equal the Falcon accounts in the trial balance.",
        "سجل الأصول في تاريخ التحويل، صف لكل أصل (أو لكل فئة عند عدم توفر التفاصيل). يجب أن تساوي التكلفة "
        "ومجمع الاستهلاك حسابات فالكون في ميزان المراجعة.",
        (
            ENTITY,
            C("asset_code", "Asset code (legacy)", "رمز الأصل (السابق)", required=True, width=12, max_len=40),
            C("description_ar", "Description (Arabic)", "الوصف (بالعربية)", required=True, width=28),
            C("description_en", "Description (English)", "الوصف (بالإنجليزية)", width=28),
            C("asset_class", "Asset class", "فئة الأصل", "choice", required=True, choices="asset_class", width=22),
            C("usage", "Usage", "الاستخدام", "choice", required=True, choices="asset_usage", width=20),
            C("location", "Location", "الموقع", required=True, width=18),
            C("serial_or_plate", "Serial or plate number", "الرقم التسلسلي أو رقم اللوحة", width=16, max_len=60),
            C("custodian_employee_no", "Custodian (employee number)", "العهدة (الرقم الوظيفي)", width=12, max_len=20),
            C("acquisition_date", "Acquisition date", "تاريخ الاقتناء", "date", required=True, width=13),
            C("available_for_use_date", "Available for use date", "تاريخ الجاهزية للاستخدام", "date", width=13),
            C("cost", "Cost (SAR)", "التكلفة (ريال)", "sar", required=True),
            C("accumulated_depreciation", "Accumulated depreciation (SAR)", "مجمع الاستهلاك (ريال)", "sar"),
            C("residual_value", "Residual value (SAR)", "القيمة المتبقية (ريال)", "sar"),
            C("method", "Depreciation method", "طريقة الاستهلاك", "choice", choices="depreciation_method", width=16),
            C("useful_life_months", "Useful life (months)", "العمر الإنتاجي (أشهر)", "int", width=10),
            C("remaining_life_months", "Remaining life (months)", "العمر المتبقي (أشهر)", "int", width=10),
            C("cost_code", "Falcon cost account code", "كود حساب التكلفة في فالكون", "legacy_code", required=True, width=12),
            C("accum_code", "Falcon accumulated depreciation code", "كود حساب مجمع الاستهلاك في فالكون", "legacy_code",
              width=12),
        ),
        examples=(
            {"entity": "EXA", "asset_code": "EX-FA-01", "description_ar": "سيارة مثال", "description_en": "Example car",
             "asset_class": "vehicle", "usage": "own_use", "location": "Example HQ", "serial_or_plate": "EX 9999",
             "custodian_employee_no": "EX-001", "acquisition_date": date(2024, 1, 15),
             "available_for_use_date": date(2024, 1, 20), "cost": D("100000.00"),
             "accumulated_depreciation": D("40000.00"), "residual_value": D("0.00"), "method": "straight_line",
             "useful_life_months": 60, "remaining_life_months": 36, "cost_code": "99151", "accum_code": "99159"},
            {"entity": "EXA", "asset_code": "EX-FA-02", "description_ar": "أرض مثال", "description_en": "Example land",
             "asset_class": "land", "usage": "own_use", "location": "Example plot", "serial_or_plate": None,
             "custodian_employee_no": None, "acquisition_date": date(2019, 5, 1), "available_for_use_date": None,
             "cost": D("500000.00"), "accumulated_depreciation": None, "residual_value": None, "method": None,
             "useful_life_months": None, "remaining_life_months": None, "cost_code": "99150", "accum_code": None},
        ),
        unique=(("entity", "asset_code"),),
    ),
)
SHEET_BY_KEY = {s.key: s for s in SHEETS}

# --- the other sheets ----------------------------------------------------------------------------------------------

INSTRUCTIONS = (
    # (English, Arabic); a None pair is a blank line, a heading starts with "#".
    ("# Almiras migration workbook", "# ملف ترحيل بيانات الميراس"),
    ("Purpose: one workbook holds everything the ERP needs from the legacy system (Falcon) at the cut-over date. "
     "It is checked by the ERP before anything is loaded.",
     "الغرض: ملف واحد يضم كل ما يحتاجه النظام من النظام السابق (فالكون) في تاريخ التحويل، ويفحصه النظام قبل "
     "تحميل أي بيانات."),
    None,
    ("# Before you start", "# قبل البدء"),
    ("1. Work on a copy of this template. Keep the filled copy on restricted company storage (it holds financial "
     "and personal data). Do not send it by e-mail and never add it to the code repository.",
     "1. اعمل على نسخة من هذا القالب. احفظ النسخة المعبأة في مساحة تخزين محمية للشركة (فيها بيانات مالية "
     "وشخصية). لا ترسلها بالبريد الإلكتروني ولا تضفها إلى مستودع الشيفرة."),
    ("2. Fill the Entities sheet first: its entity codes appear in the dropdowns of the other sheets.",
     "2. عبئ ورقة الكيانات أولًا: رموز الكيانات فيها تظهر في القوائم المنسدلة في باقي الأوراق."),
    ("3. Delete the grey example rows (row 6 onwards) before you enter your data. They are fake.",
     "3. احذف صفوف الأمثلة الرمادية (من الصف 6) قبل إدخال بياناتك. هي بيانات وهمية."),
    None,
    ("# How to fill", "# طريقة التعبئة"),
    ("Do not change rows 1 to 5 of a sheet (titles, headers and the grey field-key row). Enter data from row 6, one "
     "record per row. Do not add, remove or reorder columns.",
     "لا تغيّر الصفوف من 1 إلى 5 في أي ورقة (العناوين والصف الرمادي لمفاتيح الحقول). أدخل البيانات من الصف 6، "
     "سجل واحد في كل صف. لا تضف أعمدة ولا تحذفها ولا تغيّر ترتيبها."),
    ("* = required column (orange header). [PD] = personal data (purple header): fill only in the secured copy.",
     "* = عمود مطلوب (عنوان برتقالي). [PD] = بيانات شخصية (عنوان بنفسجي): تُعبأ في النسخة المحمية فقط."),
    ("Use the dropdowns where a column has one. Dates as YYYY-MM-DD (e.g. 2026-12-31). SAR amounts with at most two "
     "decimals; foreign-currency amounts with at most the currency's decimals (USD 2, KWD 3, JPY 0). No formulas, "
     "no merged cells, no totals rows.",
     "استخدم القوائم المنسدلة حيثما وُجدت. التواريخ بصيغة سنة-شهر-يوم (مثل 2026-12-31). المبالغ بالريال بخانتين "
     "عشريتين كحد أقصى؛ ومبالغ العملات الأجنبية بخانات العملة (الدولار 2، الدينار الكويتي 3، الين 0). بلا معادلات "
     "ولا خلايا مدمجة ولا صفوف مجاميع."),
    ("Account codes, VAT numbers, CR numbers, IBANs and ID numbers are text: keep leading zeros and all digits.",
     "أكواد الحسابات والأرقام الضريبية وأرقام السجلات والآيبان وأرقام الهويات نصوص: احتفظ بالأصفار في البداية "
     "وبجميع الأرقام."),
    None,
    ("# Sheets", "# الأوراق"),
    *[(f"{s.name}: {s.intro_en}", f"{s.title_ar}: {s.intro_ar}") for s in SHEETS],
    ("Checklist: who fills each part, who approves it, and the final sign-off.",
     "قائمة التحقق: من يعبئ كل جزء ومن يعتمده والتوقيع النهائي."),
    ("ERP chart: the group chart of accounts in the ERP, for the Account mapping sheet (reference only).",
     "دليل حسابات النظام: دليل حسابات المجموعة في النظام، لورقة ربط الحسابات (للاطلاع فقط)."),
    ("Lists: the values of the dropdowns (reference only).", "القوائم: قيم القوائم المنسدلة (للاطلاع فقط)."),
    None,
    ("# Rules the ERP checks", "# القواعد التي يفحصها النظام"),
    ("The trial balance of each entity balances to the halala; every account with a balance is mapped to an ERP "
     "account; open receivables and payables equal the customer and vendor accounts; bank book balances, fixed "
     "assets and the end-of-service provision equal their accounts in the trial balance.",
     "يتوازن ميزان المراجعة لكل كيان حتى الهللة؛ وكل حساب له رصيد مربوط بحساب في النظام؛ وتساوي الذمم المدينة "
     "والدائنة المفتوحة حسابات العملاء والموردين؛ وتساوي الأرصدة الدفترية للبنوك والأصول الثابتة ومخصص نهاية "
     "الخدمة حساباتها في ميزان المراجعة."),
    None,
    ("# Next steps", "# الخطوات التالية"),
    ("Fill → validate (migration_workbook_check) → approve the account mapping (second person) → load drafts → "
     "second person posts → tie-out report signed off. See docs/migration/README.md.",
     "التعبئة ← الفحص (migration_workbook_check) ← اعتماد ربط الحسابات (شخص آخر) ← تحميل المسودات ← الترحيل "
     "بواسطة شخص آخر ← توقيع تقرير المطابقة. انظر docs/migration/README.md."),
)

CHECKLIST_COLUMNS = (
    ("no", "#", "#", 5), ("step_en", "Step", "الخطوة", 46), ("step_ar", "Step (Arabic)", "الخطوة (بالعربية)", 46),
    ("fills", "Fills / prepares", "يعبئ / يُعد", 22), ("approves", "Reviews / approves", "يراجع / يعتمد", 22),
    ("name", "Name", "الاسم", 18), ("date", "Date", "التاريخ", 12), ("signature", "Signature / initials", "التوقيع", 16),
    ("status", "Status", "الحالة", 16), ("notes", "Notes", "ملاحظات", 30),
)
CHECKLIST_STEPS = (
    ("Entities and cut-over date per entity confirmed", "تأكيد الكيانات وتاريخ التحويل لكل كيان",
     "Project owner or CFO / مالك المشروع أو المدير المالي", "CEO of each entity / الرئيس التنفيذي لكل كيان"),
    ("Falcon chart of accounts exported and entered", "تصدير دليل حسابات فالكون وإدخاله", "Accountant / المحاسب", "CFO / المدير المالي"),
    ("Trial balance at cut-over entered; balances", "إدخال ميزان المراجعة في تاريخ التحويل ومطابقته", "Accountant / المحاسب", "CFO / المدير المالي"),
    ("Account mapping prepared", "إعداد ربط الحسابات", "Accountant / المحاسب", "CFO, in the ERP, not the preparer / المدير المالي في النظام، وليس المُعد"),
    ("Customers and open receivables entered; tie to receivables", "إدخال العملاء والذمم المدينة ومطابقتها",
     "Receivables accountant / محاسب الذمم المدينة", "CFO / المدير المالي"),
    ("Vendors and open payables entered; tie to payables", "إدخال الموردين والذمم الدائنة ومطابقتها",
     "Payables accountant / محاسب الذمم الدائنة", "CFO / المدير المالي"),
    ("Bank accounts entered; statements at cut-over attached", "إدخال الحسابات البنكية وإرفاق كشوف تاريخ التحويل",
     "Treasury or accountant / الخزينة أو المحاسب", "CFO / المدير المالي"),
    ("Employees entered in the secured copy (personal data)", "إدخال الموظفين في النسخة المحمية (بيانات شخصية)",
     "HR / الموارد البشرية", "HR manager or CEO / مدير الموارد البشرية أو الرئيس التنفيذي"),
    ("Bai' ajel contracts entered; tie to 1240 and 1241", "إدخال عقود البيع الآجل ومطابقتها مع 1240 و1241",
     "Payroll officer / مسؤول الرواتب", "CFO / المدير المالي"),
    ("Fixed asset register entered; ties to cost and depreciation", "إدخال سجل الأصول الثابتة ومطابقته",
     "Accountant / المحاسب", "CFO / المدير المالي"),
    ("Validation run: no errors; every warning decided", "تشغيل الفحص: لا أخطاء، وقرار في كل تنبيه", "Accountant / المحاسب", "CFO / المدير المالي"),
    ("Opening drafts loaded in the ERP (dry run first)", "تحميل المسودات الافتتاحية في النظام (بتجربة أولًا)",
     "Accountant / المحاسب", "-"),
    ("Sub-ledgers loaded (open items, banks, employees, provisions, leave, bai' ajel); bank accounts and leave "
     "approved", "تحميل السجلات الفرعية (البنود المفتوحة والبنوك والموظفين والمخصصات والإجازات والبيع الآجل) واعتماد "
     "الحسابات البنكية والإجازات", "Accountant, HR, payroll / المحاسب والموارد البشرية والرواتب",
     "Approver / المعتمد"),
    ("Opening drafts posted by a second person", "ترحيل المسودات بواسطة شخص آخر", "Approver, not the preparer / المعتمد، وليس المُعد", "-"),
    ("Tie-out report: ERP = legacy to the halala, signed", "تقرير المطابقة: النظام = النظام السابق حتى الهللة، موقع",
     "Accountant / المحاسب", "CFO and CEO / المدير المالي والرئيس التنفيذي"),
    ("Filled workbook archived securely; working copies deleted", "حفظ الملف المعبأ بأمان وحذف النسخ المؤقتة",
     "Accountant / المحاسب", "Internal audit / المراجعة الداخلية"),
)
SIGN_OFF = (("Prepared by", "أعده"), ("Reviewed by", "راجعه"), ("Approved by (CEO / authorised person)",
                                                                  "اعتمده (الرئيس التنفيذي أو المفوض)"))
