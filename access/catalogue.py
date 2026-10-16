"""Permission catalogue and starting roles (spec §7, D13).

Plain data so the data migration and the `seed_roles` command use the same lists.
The first segment of a permission code is its module; module-level scoping of a role
(D13) is simply which module permissions the role holds.
"""

PERMISSIONS = [
    # code, English, Arabic
    ("core.entity.view", "See the entity", "عرض المنشأة"),
    ("access.user.manage", "Create and deactivate users", "إنشاء المستخدمين وتعطيلهم"),
    ("access.entity_access.manage", "Grant and revoke roles in the entity", "منح الأدوار وسحبها في المنشأة"),
    ("access.audit.view", "Read the audit log of the entity", "الاطلاع على سجل التدقيق للمنشأة"),
    ("approvals.request.view", "See approval requests of the entity", "عرض طلبات الموافقة للمنشأة"),
    ("approvals.request.view_own", "See own approval requests only", "عرض طلبات الموافقة الخاصة فقط"),
    ("approvals.request.submit", "Submit own drafts for approval", "تقديم المسودات الخاصة للموافقة"),
    ("approvals.request.act", "Approve, reject or return when eligible", "الموافقة أو الرفض أو الإعادة عند الأهلية"),
    ("approvals.request.cancel_any", "Cancel others' pending requests", "إلغاء طلبات الآخرين المعلقة"),
    ("approvals.policy.manage", "Edit the approval matrix", "تعديل مصفوفة الموافقات"),
    ("approvals.delegation.manage_own", "Create delegations for oneself", "إنشاء تفويضات للنفس"),
    ("approvals.delegation.manage_any", "Create delegations for others", "إنشاء تفويضات للآخرين"),
    ("ledger.entry.view", "View journal entries", "عرض القيود اليومية"),
    ("ledger.entry.create", "Create draft journal entries", "إنشاء مسودات القيود اليومية"),
    ("ledger.entry.post", "Post journal entries after approval", "ترحيل القيود بعد الموافقة"),
    ("documents.document.upload", "Upload documents", "رفع المستندات"),
    ("investments.investment.view", "View investments", "عرض الاستثمارات"),
    ("deals.deal.view", "View deals", "عرض الصفقات"),
    ("reports.report.view", "View reports", "عرض التقارير"),
]

ALL = [code for code, _, _ in PERMISSIONS]

_READ_FINANCE = ["core.entity.view", "approvals.request.view", "ledger.entry.view", "reports.report.view"]

ROLES = {
    "managing_partner": ("Managing Partner", "الشريك المدير", ALL),
    "chairman": (
        "Chairman", "رئيس مجلس الإدارة",
        _READ_FINANCE + [
            "access.audit.view", "approvals.request.act", "approvals.delegation.manage_own",
            "investments.investment.view", "deals.deal.view",
        ],
    ),
    # CFO: not in the spec's §7.2 table. Conservative build choice (see Build notes):
    # accountant rights plus audit read; no approve/post until a policy decision says so.
    "cfo": (
        "Chief Financial Officer", "المدير المالي",
        _READ_FINANCE + ["access.audit.view", "approvals.request.submit", "ledger.entry.create"],
    ),
    "accountant": (
        "Accountant", "المحاسب",
        _READ_FINANCE + ["approvals.request.submit", "ledger.entry.create"],
    ),
    # D13: limited modules (investments, deals, reports), read only. No ledger, no approvals.
    "business_analyst": (
        "Business Analyst", "محلل الأعمال",
        ["core.entity.view", "investments.investment.view", "deals.deal.view", "reports.report.view"],
    ),
    # D13: PA prepares drafts and uploads documents; never approves or posts.
    "pa_drafter": (
        "PA (drafts and documents)", "المساعد الشخصي (مسودات ومستندات)",
        [
            "core.entity.view", "approvals.request.view_own", "approvals.request.submit",
            "ledger.entry.create", "documents.document.upload",
        ],
    ),
    "auditor": (
        "External Auditor (read-only)", "المراجع الخارجي (قراءة فقط)",
        _READ_FINANCE + ["access.audit.view"],
    ),
    "viewer": ("Viewer", "مشاهد", ["core.entity.view", "approvals.request.view", "ledger.entry.view"]),
    # R18: drafts only, never act.
    "agent_drafter": (
        "AI agent (drafts only)", "وكيل ذكاء اصطناعي (مسودات فقط)",
        ["core.entity.view", "approvals.request.view_own", "approvals.request.submit", "ledger.entry.create"],
    ),
}

# Permissions a service agent may never hold, whatever its roles (R18). Checked in has_perm.
AGENT_FORBIDDEN = {
    "approvals.request.act",
    "approvals.request.cancel_any",
    "approvals.policy.manage",
    "approvals.delegation.manage_own",
    "approvals.delegation.manage_any",
    "access.user.manage",
    "access.entity_access.manage",
    "ledger.entry.post",
}


def seed(Permission, Role):
    """Create missing permissions and system roles; add missing permissions to system roles.

    Never removes a permission from an existing role (that is an access change made by a person).
    """
    perms = {}
    for code, en, ar in PERMISSIONS:
        perm, _ = Permission.objects.get_or_create(code=code, defaults={"name_en": en, "name_ar": ar})
        perms[code] = perm
    for code, (en, ar, codes) in ROLES.items():
        role, _ = Role.objects.get_or_create(code=code, defaults={"name_en": en, "name_ar": ar, "is_system": True})
        role.permissions.add(*[perms[c] for c in codes])
    return len(perms), len(ROLES)
