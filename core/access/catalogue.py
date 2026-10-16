"""Permission catalogue (access.md 3.6). Code, not data: flags cannot be edited in admin.

Flags:
- ``A``  approval: creator / submitter may not approve (R14)
- ``S!`` segregation that cannot be switched off (R15)
- ``DC`` dual control (R19)
- ``G``  group-level only (only group-scope roles may hold it)
- ``Ag`` an agent role may hold it (R12)
- ``P``  a grant for it authorises a ledger posting (R23)
``system`` is set for "Sys:x" permissions: only system user ``x`` may hold them (R13).

Arabic labels are marked for review [AR-review] like the role names in the spec.
"""
from dataclasses import dataclass, field

APPROVAL = "A"
SEGREGATED = "S!"
DUAL_CONTROL = "DC"
GROUP_ONLY = "G"
AGENT_OK = "Ag"
POSTS = "P"

FLAGS = frozenset({APPROVAL, SEGREGATED, DUAL_CONTROL, GROUP_ONLY, AGENT_OK, POSTS})
# Flags an agent user can never use, whatever its role says (R12, A7).
AGENT_FORBIDDEN_FLAGS = frozenset({APPROVAL, SEGREGATED, DUAL_CONTROL, GROUP_ONLY, POSTS})

SYSTEM_CODES = ("billing", "zatca_queue", "fx_import")


@dataclass(frozen=True)
class Perm:
    codename: str
    label_en: str
    label_ar: str
    flags: frozenset = field(default_factory=frozenset)
    system: str | None = None

    @property
    def app_label(self):
        return self.codename.split(".", 1)[0]

    @property
    def is_approval(self):
        return APPROVAL in self.flags

    @property
    def is_segregated(self):
        return SEGREGATED in self.flags

    @property
    def is_dual_control(self):
        return DUAL_CONTROL in self.flags

    @property
    def is_group_only(self):
        return GROUP_ONLY in self.flags

    @property
    def agent_ok(self):
        return AGENT_OK in self.flags and not (self.flags & AGENT_FORBIDDEN_FLAGS) and self.system is None

    @property
    def posts(self):
        return POSTS in self.flags

    @property
    def is_view(self):
        """Read-only permissions; these still work on an inactive entity (OQ-8 default)."""
        return self.codename.split(".", 1)[1].startswith("view")


def _p(codename, en, ar, *flags, system=None):
    unknown = set(flags) - FLAGS
    if unknown:
        raise ValueError(f"Unknown flags {unknown} on {codename}")
    if system is not None and system not in SYSTEM_CODES:
        raise ValueError(f"Unknown system code {system} on {codename}")
    return Perm(codename, en, ar, frozenset(flags), system)


_PERMS = [
    # core
    _p("core.view_access", "View memberships and access log", "عرض العضويات وسجل الوصول"),
    _p("core.grant_membership", "Grant and revoke memberships", "منح العضويات وإلغاؤها", SEGREGATED),
    _p("core.delegate_approval", "Delegate approval", "تفويض صلاحية الاعتماد", SEGREGATED),
    # invoicing
    _p("invoicing.manage_customer", "Manage customers", "إدارة العملاء"),
    _p("invoicing.manage_vendor", "Manage vendors", "إدارة الموردين"),
    _p("invoicing.create_sales_document", "Create and edit draft sales documents", "إنشاء مسودات مستندات المبيعات وتعديلها", AGENT_OK),
    _p("invoicing.submit_sales_document", "Submit sales documents", "تقديم مستندات المبيعات"),
    _p("invoicing.approve_sales_document", "Approve and issue sales documents", "اعتماد مستندات المبيعات وإصدارها", APPROVAL, POSTS),
    _p("invoicing.create_from_billing", "Create invoices from billing", "إنشاء الفواتير من نظام الفوترة", system="billing"),
    _p("invoicing.post_on_clearance", "Post sales on ZATCA clearance", "ترحيل المبيعات عند الاعتماد من الهيئة", POSTS, system="zatca_queue"),
    _p("invoicing.create_bill", "Create draft bills", "إنشاء مسودات فواتير الموردين", AGENT_OK),
    _p("invoicing.create_vendor_payment", "Create vendor payments", "إنشاء مدفوعات الموردين"),
    _p("invoicing.submit_bill", "Submit bills", "تقديم فواتير الموردين"),
    _p("invoicing.submit_vendor_payment", "Submit vendor payments", "تقديم مدفوعات الموردين"),
    _p("invoicing.approve_bill", "Approve bills", "اعتماد فواتير الموردين", APPROVAL, POSTS),
    _p("invoicing.approve_vendor_payment", "Approve vendor payments", "اعتماد مدفوعات الموردين", APPROVAL, POSTS),
    _p("invoicing.reverse_document", "Reverse documents", "عكس المستندات", POSTS),
    _p("invoicing.post_receipt", "Post customer receipts", "ترحيل المقبوضات من العملاء", POSTS),
    _p("invoicing.allocate", "Allocate payments", "تخصيص الدفعات"),
    _p("invoicing.enter_wht_data", "Enter withholding tax data", "إدخال بيانات ضريبة الاستقطاع"),
    _p("invoicing.verify_wht_data", "Verify withholding tax data", "التحقق من بيانات ضريبة الاستقطاع"),
    _p("invoicing.manage_settings", "Manage invoicing settings", "إدارة إعدادات الفوترة"),
    _p("invoicing.post_purification_transfer", "Post purification transfers", "ترحيل تحويلات التطهير", POSTS),
    _p("invoicing.approve_purification_payout", "Approve purification payouts", "اعتماد صرف مبالغ التطهير", APPROVAL, POSTS),
    _p("invoicing.view", "View invoicing", "عرض الفوترة", AGENT_OK),
    _p("invoicing.view_personal_data", "View personal data", "عرض البيانات الشخصية"),
    # billing
    _p("billing.edit_contract_draft", "Edit draft contracts", "تعديل مسودات العقود", AGENT_OK),
    _p("billing.record_contract_event", "Record contract events", "تسجيل أحداث العقود"),
    _p("billing.approve_contract", "Approve contracts", "اعتماد العقود", APPROVAL),
    _p("billing.approve_termination", "Approve contract terminations", "اعتماد إنهاء العقود", APPROVAL),
    _p("billing.start_run", "Start billing runs", "بدء دورات الفوترة"),
    _p("billing.release_period", "Release billing periods", "إطلاق فترات الفوترة"),
    _p("billing.issue_from_contract", "Issue invoices from contracts", "إصدار الفواتير من العقود", system="billing"),
    _p("billing.create_credit_note_draft", "Create draft credit notes", "إنشاء مسودات الإشعارات الدائنة"),
    _p("billing.view", "View billing", "عرض العقود والفوترة", AGENT_OK),
    _p("billing.view_employee_ref", "View employee references", "عرض مراجع الموظفين"),
    # zatca
    _p("zatca.manage_profile", "Manage ZATCA profile and EGS units", "إدارة ملف الربط ووحدات الفوترة"),
    _p("zatca.onboard", "Onboard and manage ZATCA certificates", "التسجيل وإدارة شهادات الهيئة", SEGREGATED),
    _p("zatca.switch_to_production", "Switch an EGS unit to production", "تحويل وحدة الفوترة إلى بيئة الإنتاج", DUAL_CONTROL),
    _p("zatca.process_queue", "Process the ZATCA queue", "معالجة طابور الإرسال للهيئة", system="zatca_queue"),
    _p("zatca.retry_failed", "Retry failed ZATCA submissions", "إعادة محاولة الإرسال المتعثر للهيئة"),
    _p("zatca.view", "View ZATCA", "عرض بيانات الربط مع الهيئة"),
    _p("zatca.view_status", "View ZATCA status", "عرض حالة الإرسال للهيئة", AGENT_OK),
    _p("zatca.export_archive", "Export the ZATCA archive", "تصدير أرشيف الفواتير الإلكترونية"),
    _p("zatca.close_late_notice", "Close late-submission notices", "إغلاق إشعارات التأخر في الإرسال"),
    _p("zatca.grant_key_delegation", "Grant ZATCA key custody", "تفويض حفظ مفاتيح الهيئة", GROUP_ONLY, SEGREGATED),
    # ledger
    _p("ledger.create_entry", "Create draft journal entries", "إنشاء مسودات القيود اليومية", AGENT_OK),
    _p("ledger.post_entry", "Post manual journal entries", "ترحيل القيود اليومية اليدوية", APPROVAL, POSTS),
    _p("ledger.reverse_entry", "Reverse journal entries", "عكس القيود اليومية", POSTS),
    _p("ledger.close_period", "Close fiscal periods", "إقفال الفترات المالية"),
    _p("ledger.maintain_global_rate", "Maintain group exchange rates", "إدارة أسعار الصرف للمجموعة", GROUP_ONLY),
    _p("ledger.import_global_rate", "Import group exchange rates", "استيراد أسعار الصرف للمجموعة", GROUP_ONLY, system="fx_import"),
    _p("ledger.maintain_entity_rate", "Maintain entity exchange rates", "إدارة أسعار الصرف للكيان"),
    _p("ledger.use_manual_rate", "Use a manual exchange rate", "استخدام سعر صرف يدوي"),
    _p("ledger.revalue", "Run currency revaluation", "تشغيل إعادة تقييم العملات"),
    _p("ledger.view", "View the ledger", "عرض دفتر الأستاذ", AGENT_OK),
    _p("ledger.view_rates", "View exchange rates", "عرض أسعار الصرف", AGENT_OK),
    _p("ledger.view_translation", "View currency translation", "عرض ترجمة العملات", AGENT_OK),
    _p("ledger.change_functional_currency", "Change functional currency", "تغيير العملة الوظيفية", GROUP_ONLY),
]

CATALOGUE = {p.codename: p for p in _PERMS}
assert len(CATALOGUE) == len(_PERMS), "duplicate codename in catalogue"

# Plain (non-Sys) permissions a system user may also hold (3.6 holders column, R13).
SYSTEM_EXTRA = {
    "billing": frozenset({"billing.start_run"}),
    "zatca_queue": frozenset(),
    "fx_import": frozenset(),
}

# Ledger postings (R23): which P grants each ledger service accepts.
REVERSE_PERMISSIONS = frozenset({"ledger.reverse_entry", "invoicing.reverse_document"})
POST_PERMISSIONS = frozenset(c for c, p in CATALOGUE.items() if p.posts) - {"ledger.reverse_entry"}


def get(codename):
    try:
        return CATALOGUE[codename]
    except KeyError:
        raise KeyError(f"Permission {codename!r} is not in the access catalogue.") from None


def allowed_for_system(system_code):
    """Every permission system user ``system_code`` may hold (R13)."""
    own = {c for c, p in CATALOGUE.items() if p.system == system_code}
    return frozenset(own) | SYSTEM_EXTRA.get(system_code, frozenset())


def role_may_hold(kind, scope, system_code, perm):
    """Return None if a role of this kind/scope may hold ``perm``, else a reason code (R12, R13, G)."""
    if perm.is_group_only and scope != "group":
        return "group_only"
    if kind == "agent":
        return None if perm.agent_ok else "agent_forbidden"
    if kind == "system":
        return None if perm.codename in allowed_for_system(system_code) else "system_forbidden"
    if perm.system is not None:
        return "system_only"
    return None
