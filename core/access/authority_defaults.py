"""Approval activities and their code defaults (access.md §10.5, R61, R70; payroll.md §11 R82, R83).

Every approvable document type is one **activity**. An entity's authority matrix (``AuthorityRule`` rows of its
effective ``AuthorityMatrixVersion``) says who approves each activity. Where the entity has **no row at all** for an
activity, the engine uses the activity's **code default** below, which reproduces the behaviour before the matrix
existed (R70):

- ``roles=None``: every holder of the activity's approval permission **in that entity** (no cascade from a group
  membership on the group parent, R64; so the managing partner's fallback of 3.6 applies only in the group parent);
- ``roles=(...)``: only holders through these roles (e.g. payroll runs: ``hr_officer``, never the managing partner);
- ``top_authority=True``: only holders through the entity's top-authority role (``EntityGovernance``:
  ``managing_partner`` in the group parent, ``ceo`` elsewhere; OQ-39).

Each module keeps its own extra checks on top (payroll's overtime route and own-pay rules, the tax module's three
people, hr's "not about yourself", ...). The request records ``rule_source = default`` and the monthly governance
report lists such approvals (R84 section 4).

The list is code, not data: ``ApprovalActivity`` rows mirror it (seeded by migration, like roles), so matrix rows can
point at them. ``pending=True`` marks an activity whose module has not built the document yet (its permission may
not be in the catalogue yet); the engine has no adapter for it until the module registers one.
"""
from dataclasses import dataclass, field

DOCUMENT_TOTAL = "document_total"
AMOUNT = "amount"
PRINCIPAL = "principal"
NONE = "none"
AMOUNT_BASES = (
    (DOCUMENT_TOTAL, "Document total gross of VAT / إجمالي المستند شاملًا الضريبة"),
    (AMOUNT, "Document amount / مبلغ المستند"),
    (PRINCIPAL, "Loan principal or sale price / أصل القرض أو سعر البيع"),
    (NONE, "No amount (one band only) / بلا مبلغ (نطاق واحد)"),
)


@dataclass(frozen=True)
class Default:
    """Who approves when the entity has no matrix row for the activity (R70)."""

    roles: tuple | None = None
    top_authority: bool = False
    note_en: str = ""
    note_ar: str = ""

    def describe(self):
        if self.top_authority:
            return "top authority / السلطة العليا"
        if self.roles:
            return ", ".join(self.roles)
        return "holders of the permission in the entity / حاملو الصلاحية في الكيان"


@dataclass(frozen=True)
class Activity:
    code: str
    name_en: str
    name_ar: str
    permissions: tuple
    amount_basis: str = DOCUMENT_TOTAL
    default: Default = field(default_factory=Default)
    pending: bool = False

    @property
    def permission(self):
        """The main approval permission (``ApprovalActivity.permission``)."""
        return self.permissions[0]


HOLDERS = Default()
TOP = Default(top_authority=True, note_en="Almiras: the managing partner; other entities: their CEO (OQ-39)",
              note_ar="الماس: الشريك المدير؛ الكيانات الأخرى: رئيسها التنفيذي")
# Payroll's own seed rows (payroll.md §7, §11 R82; payroll/approvals.py): the CFO (approver), the CEO as a parallel
# role (OQ-41) and, in the group parent, the managing partner.
_APPROVER = Default(roles=("approver", "ceo", "managing_partner"))


def _a(code, en, ar, *permissions, basis=DOCUMENT_TOTAL, default=HOLDERS, pending=False):
    return Activity(code, en, ar, tuple(permissions), basis, default, pending)


_ACTIVITIES = [
    # ledger
    _a("ledger.manual_entry", "Manual journal entries", "القيود اليومية اليدوية", "ledger.post_entry", basis=NONE),
    # invoicing
    _a("invoicing.sales_document", "Sales documents (invoices, credit and debit notes)",
       "مستندات المبيعات (الفواتير والإشعارات الدائنة والمدينة)", "invoicing.approve_sales_document"),
    _a("invoicing.bill", "Vendor bills and vendor credit notes", "فواتير الموردين وإشعاراتهم الدائنة",
       "invoicing.approve_bill"),
    _a("invoicing.vendor_payment", "Vendor payments", "مدفوعات الموردين", "invoicing.approve_vendor_payment",
       basis=AMOUNT),
    _a("invoicing.purification_funding", "Purification account fundings", "تمويل حساب التطهير",
       "invoicing.approve_purification_funding", basis=AMOUNT),
    _a("invoicing.purification_payout", "Purification payouts", "صرف مبالغ التطهير",
       "invoicing.approve_purification_payout", basis=AMOUNT),
    _a("invoicing.charity_recipient", "Charity recipients of purification payouts", "الجهات الخيرية المستفيدة من التطهير",
       "invoicing.approve_charity_recipient", basis=NONE),
    _a("invoicing.recovery_ratio", "Input VAT recovery ratios (Art. 51)", "نسب استرداد ضريبة المدخلات (المادة 51)",
       "invoicing.approve_recovery_ratio", basis=NONE, pending=True),
    _a("invoicing.art51_true_up", "Art. 51 true-ups", "تسويات المادة 51", "invoicing.approve_art51_true_up",
       basis=AMOUNT, pending=True),
    _a("invoicing.deferred_release", "Deferred income releases", "إطلاق الإيرادات المؤجلة",
       "invoicing.approve_deferred_release", basis=AMOUNT, pending=True),
    _a("invoicing.deposit_application", "Deposit applications", "تطبيق الدفعات المقدمة",
       "invoicing.approve_deposit_application", basis=AMOUNT, pending=True),
    # billing
    _a("billing.contract", "Contracts", "العقود", "billing.approve_contract"),
    _a("billing.version", "Contract versions (amendments, terminations, cancellations)",
       "نسخ العقود (التعديلات والإنهاء والإلغاء)", "billing.approve_version", "billing.approve_termination"),
    _a("billing.timesheet", "Timesheets", "سجلات الوقت", "billing.approve_timesheet", basis=NONE, pending=True),
    _a("billing.cost_pool_period", "Cost pool periods", "فترات مجمعات التكلفة", "billing.approve_cost_pool_period",
       basis=NONE, pending=True),
    # hr (R82: nobody approves a record about themselves; hr's own check stays)
    _a("hr.contract", "Employment contracts", "عقود العمل", "hr.approve_contract", basis=NONE),
    _a("hr.assignment", "Assignments and secondments", "التكليفات والإعارات", "hr.approve_assignment", basis=NONE),
    _a("hr.leave", "Leave requests", "طلبات الإجازات", "hr.approve_leave", basis=NONE),
    _a("hr.leave_adjustment", "Leave balance adjustments", "تسويات أرصدة الإجازات", "hr.adjust_leave_balance", basis=NONE),
    _a("hr.termination", "Terminations", "إنهاء الخدمة", "hr.approve_termination", basis=NONE),
    _a("hr.iban_change", "Employee IBAN changes (second person)", "تغييرات آيبان الموظفين (شخص ثانٍ)",
       "hr.manage_employee", basis=NONE),
    _a("hr.statutory_parameter", "Statutory parameter verification (group parent)",
       "التحقق من المعايير النظامية (الكيان الأم)", "hr.verify_statutory_parameter", basis=NONE),
    # payroll (payroll.md §11 R82 table)
    _a("payroll.run", "Payroll runs (regular and off-cycle)", "مسيرات الرواتب (الدورية وخارج الدورة)",
       "payroll.approve_run", basis=NONE,
       default=Default(roles=("hr_officer",), note_en="Contracted payroll needs no managing partner approval (P20(a))",
                       note_ar="الرواتب التعاقدية لا تحتاج اعتماد الشريك المدير")),
    _a("payroll.run_reversal", "Payroll run reversals", "عكس مسيرات الرواتب", "payroll.reverse_run", basis=NONE,
       default=_APPROVER),
    _a("payroll.input", "Payroll inputs", "مدخلات الرواتب", "payroll.approve_input", basis=NONE,
       default=Default(roles=("payroll_officer", "managing_partner"))),
    _a("payroll.overtime", "Overtime", "العمل الإضافي", "payroll.approve_overtime", basis=NONE,
       default=Default(note_en="HrSettings.overtime_approval_route: managing partner or department head (payroll R45)",
                       note_ar="مسار اعتماد العمل الإضافي في إعدادات الموارد البشرية")),
    _a("payroll.maternity_clearance", "Maternity legal clearances", "الإذن القانوني لأجر الأمومة",
       "payroll.approve_maternity_clearance", basis=NONE, default=_APPROVER),
    _a("payroll.loan", "Interest-free employee loans", "قروض الموظفين الحسنة", "payroll.approve_loan", basis=PRINCIPAL,
       default=TOP),
    _a("payroll.deferred_sale_terms", "Bai' ajel terms", "شروط البيع الآجل", "payroll.approve_deferred_sale_terms",
       basis=NONE, default=TOP),
    _a("payroll.deferred_sale", "Bai' ajel sales and ibra' rebates", "البيع الآجل والإبراء", "payroll.approve_deferred_sale",
       basis=PRINCIPAL, default=TOP),
    _a("payroll.final_settlement", "Final settlements", "المخالصات النهائية", "payroll.approve_final_settlement",
       basis=AMOUNT, default=_APPROVER),
    _a("payroll.settlement_correction", "Final settlement corrections", "تصحيحات المخالصات النهائية",
       "payroll.approve_settlement_correction", basis=AMOUNT, default=_APPROVER),
    _a("payroll.loan_write_off", "Loan write-offs", "شطب القروض", "payroll.approve_loan_write_off", basis=AMOUNT,
       default=TOP),
    _a("payroll.payment", "Payroll payments", "مدفوعات الرواتب", "payroll.approve_payment", basis=AMOUNT,
       default=_APPROVER),
    _a("payroll.payment_reversal", "Payroll payment reversals", "عكس مدفوعات الرواتب", "payroll.reverse_payment",
       basis=AMOUNT, default=_APPROVER),
    _a("payroll.gosi_fine", "GOSI late-payment fines", "غرامات تأخير التأمينات", "payroll.approve_gosi_fine",
       basis=AMOUNT, default=_APPROVER),
    _a("payroll.cost_item", "Employee cost items", "بنود تكاليف الموظفين", "payroll.approve_cost_item", basis=AMOUNT,
       default=_APPROVER),
    _a("payroll.secondment_cost_report", "Secondment cost reports", "تقارير تكلفة الإعارة",
       "payroll.approve_cost_report", basis=NONE, default=_APPROVER),
    _a("payroll.profit_recognition", "Bai' ajel profit recognition", "إثبات أرباح البيع الآجل",
       "payroll.approve_profit_recognition", basis=AMOUNT, default=_APPROVER),
    _a("payroll.mudad_justification", "Mudad justifications", "مبررات منصة مدد", "payroll.approve_mudad_justification",
       basis=NONE, default=Default(roles=("hr_officer",)), pending=True),
    # banking
    _a("banking.bank_account", "Bank accounts", "الحسابات البنكية", "banking.approve_bank_account", basis=NONE),
    _a("banking.bank_account_change", "Bank account changes", "تعديلات الحسابات البنكية", "banking.approve_bank_account",
       basis=NONE),
    _a("banking.bank_transaction", "Bank transactions", "العمليات البنكية", "banking.approve_bank_transaction",
       basis=AMOUNT),
    _a("banking.reconciliation", "Bank reconciliations", "التسويات البنكية", "banking.approve_reconciliation", basis=NONE),
    # tax (G3: three people for VAT returns, kept by the tax module; NSA)
    _a("tax.vat_return", "VAT returns (review and approval)", "إقرارات ضريبة القيمة المضافة (المراجعة والاعتماد)",
       "tax.approve_return", "tax.review_return", basis=NONE,
       default=Default(note_en="Preparer, reviewer and approver are three people (G3, tax R8)",
                       note_ar="المُعِدّ والمراجع والمعتمد ثلاثة أشخاص")),
    _a("tax.tax_document", "Tax payments, refunds and import VAT", "الدفعات والاستردادات الضريبية وضريبة الاستيراد",
       "tax.approve_payment", basis=AMOUNT),
    _a("tax.settings", "Tax settings", "إعدادات الضرائب", "tax.change_settings", basis=NONE),
    _a("tax.box_definition", "VAT box definitions (verification)", "تعريفات خانات الإقرار (التحقق)", "tax.maintain_rates",
       basis=NONE),
    # migration
    _a("migration.account_map", "Legacy account maps", "خرائط حسابات النظام السابق", "migration.approve_account_map",
       basis=NONE),
    # access itself (R76; a dual-control request, shown in the inbox only, OQ-49)
    _a("core.authority_matrix", "Authority matrix versions", "إصدارات مصفوفة الصلاحيات", "core.approve_authority_matrix",
       basis=NONE, default=Default(note_en="R76: the top authority, or the chairman / board representative when the top "
                                           "authority proposes", note_ar="السلطة العليا، أو رئيس مجلس الإدارة / ممثل "
                                                                         "مجلس الإدارة إذا اقترحتها السلطة العليا")),
]

ACTIVITIES = {a.code: a for a in _ACTIVITIES}
assert len(ACTIVITIES) == len(_ACTIVITIES), "duplicate activity code"


def get(code):
    try:
        return ACTIVITIES[code]
    except KeyError:
        raise KeyError(f"Approval activity {code!r} is not in core/access/authority_defaults.py.") from None


def for_permission(permission):
    """Activities whose approval is checked with ``permission`` (first = the main one)."""
    return [a for a in _ACTIVITIES if permission in a.permissions]


def sync(ApprovalActivity):
    """Create or update the ``ApprovalActivity`` rows from this list (data migrations pass the historical model)."""
    for a in _ACTIVITIES:
        ApprovalActivity.objects.update_or_create(
            code=a.code, defaults=dict(name_en=a.name_en, name_ar=a.name_ar, permission=a.permission,
                                       amount_basis=a.amount_basis, is_pending=a.pending),
        )
