"""The hand-off from payroll to invoicing for the bai' ajel sale invoice (payroll R47 step 4, P19(k); invoicing
R112, E28): invoicing creates a simplified sales invoice to the employee at 15% VAT on the sale price (cost + fixed
profit; the VAT is charged to and paid by the employee, P20(h)), reports it to ZATCA and posts Dr 1240 / Cr 1250 /
Cr 1241 / Cr 2200.

Invoicing R112 is not built yet, so the default :class:`NotBuiltInvoicingPort` refuses (en/ar) and no bai' ajel sale
happens. Setting ``PAYROLL_INVOICING_PORT`` picks another port; tests use ``payroll.testing.FakeInvoicingPort``.
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.utils.module_loading import import_string

from .models import error

DEFAULT_PORT = "payroll.invoicing_port.NotBuiltInvoicingPort"


@dataclass(frozen=True)
class SaleResult:
    document_ref: str
    vat_amount: Decimal


class NotBuiltInvoicingPort:
    def create_bai_ajel_sale(self, contract, *, user):
        raise error("The bai' ajel sale invoice is created by invoicing (R112), which is not built yet.",
                    "تُنشأ فاتورة البيع الآجل من خلال الفوترة (R112) التي لم تُبنَ بعد.", code="invoicing_not_built")


_override = None


def get_port():
    if _override is not None:
        return _override
    return import_string(getattr(settings, "PAYROLL_INVOICING_PORT", DEFAULT_PORT))()


# --- review fix 4 (invoicing R122 as amended): profit is recognised only for a collection payroll approved ----------

# (document label, document pk, user pk, mode) while payroll's run approval or final-settlement approval applies its
# effects ("collect") or a run reversal gives its instalments back ("reverse"). Invoicing's profit recognition and
# its reversal are refused outside it (``collection_problems``).
_collecting = ContextVar("payroll_collecting", default=None)
COLLECTING_DOCUMENTS = {"payroll.payrollrun": "payroll.approve_run",
                        "payroll.finalsettlement": "payroll.approve_final_settlement",
                        # Review 4 (2026-10-21, item 5): a balance after exit recovered from the former employee.
                        "payroll.deferredsaleclosure": "payroll.approve_deferred_sale_closure"}
_REF = re.compile(r"^payroll\.deferredsaleinstallment:(\d+)$")


@contextmanager
def collecting(document, user, mode="collect"):
    """Set by ``payroll.services`` around the effects of ``approve_run`` / ``approve_final_settlement`` /
    ``approve_deferred_sale_closure`` (a recovery; ``collect``) and around ``reverse_run``'s profit reversals
    (``reverse``)."""
    token = _collecting.set((document._meta.label_lower, document.pk, getattr(user, "pk", None), mode))
    try:
        yield
    finally:
        _collecting.reset(token)


def collection_problems(contract, *, source_ref, amount=None, user, mode="collect"):
    """Why invoicing may not recognise (``collect``) or reverse (``reverse``) the bai' ajel profit of ``source_ref``
    for ``contract`` now: [] when the call comes from the payroll approval that collected it, ``source_ref`` names a
    deducted (run) or settled (final settlement) installment of that very contract, or one recovered after exit by
    an approved recovery (``DeferredSaleClosure``, review 4), and ``amount`` is what was collected on it. Returns
    (problems, permission the user must hold)."""
    from .models import DeferredSaleClosure, DeferredSaleInstallment, FinalSettlement

    marker = _collecting.get()
    if marker is None or marker[3] != mode or marker[2] != getattr(user, "pk", None) \
            or marker[0] not in COLLECTING_DOCUMENTS:
        return ["Bai' ajel profit is recognised (or reversed) only by the payroll run or final-settlement approval "
                "that collected the instalment (R122). / تتحقق أرباح البيع الآجل (أو تُعكس) فقط من خلال اعتماد مسير "
                "الرواتب أو المخالصة النهائية الذي حصّل القسط."], None
    label, doc_pk = marker[0], marker[1]
    permission = COLLECTING_DOCUMENTS[label] if mode == "collect" else "payroll.reverse_run"
    match = _REF.match(str(source_ref or ""))
    inst = None
    if match:
        inst = DeferredSaleInstallment.objects.filter(pk=int(match.group(1)), contract_id=contract.pk,
                                                      entity_id=contract.entity_id).first()
    wrong = ["The collection does not name an installment of this contract collected by this approval, at this "
             "amount (R122). / لا يشير التحصيل إلى قسط من هذا العقد حصّله هذا الاعتماد بهذا المبلغ."]
    if inst is None:
        return wrong, permission
    if label == "payroll.payrollrun":
        ok = inst.status == DeferredSaleInstallment.Status.DEDUCTED and inst.run_id == doc_pk
    elif label == "payroll.deferredsaleclosure":
        closure = DeferredSaleClosure.objects.filter(pk=doc_pk, contract_id=contract.pk, entity_id=contract.entity_id,
                                                     kind=DeferredSaleClosure.Kind.RECOVERY,
                                                     status=DeferredSaleClosure.Status.APPROVED).first()
        ok = (mode == "collect" and closure is not None
              and inst.status == DeferredSaleInstallment.Status.RECOVERED_AFTER_EXIT
              and any(part.get("installment_id") == inst.pk for part in closure.installment_parts or []))
    else:
        fs = FinalSettlement.objects.filter(pk=doc_pk).first()
        ok = (mode == "collect" and fs is not None and fs.employee_id == contract.employee_id
              and inst.status == DeferredSaleInstallment.Status.SETTLED_IN_FINAL)
    if not ok or (amount is not None and inst.deducted_amount != amount):
        return wrong, permission
    return [], permission


# --- review 2026-10-20b, finding 1 (invoicing R128 as amended): a rebate credit note only from payroll's approval ----

# (rebate pk, user pk, token, grant) while ``approve_deferred_sale_rebate`` asks invoicing for the rebate's credit
# note. Invoicing's ``create_deferred_sale_adjustment`` is refused outside it (``rebate_problems``). Review
# 2026-10-20c, finding 4: the marker alone is not proof (any code can build an object with a ``pk``): it carries a
# one-time token that the approval writes on the locked rebate row in its own transaction (and clears when done), and
# the approver's grant, checked again here with the authority matrix and four eyes.
_rebating = ContextVar("payroll_rebating", default=None)
_REBATE_REF = re.compile(r"^payroll\.deferredsalerebate:(\d+)$")


@contextmanager
def rebating(rebate, user, *, token, grant):
    """Set by ``payroll.services.approve_deferred_sale_rebate`` around its call to ``create_bai_ajel_rebate``, with
    the token it stored on the locked rebate row and the grant of the approver's ``payroll.approve_deferred_sale``."""
    marker = _rebating.set((rebate.pk, getattr(user, "pk", None), str(token or ""), grant))
    try:
        yield
    finally:
        _rebating.reset(marker)


def rebate_problems(contract, *, source_ref, amount, approver):
    """Why invoicing may not issue the rebate credit note ``source_ref`` of ``amount`` for ``contract`` now: [] only
    inside the payroll approval of that very rebate (``rebating``), by its approver, for a rebate of that contract and
    entity, being approved (draft, or rejected and approved again), at that amount. Review 2026-10-20c, finding 4:
    the rebate row is read again (the approval holds its lock) and must carry the marker's one-time token (written by the approval in
    this transaction); the approver's grant must be the one logged for this contract, still valid and unused, and
    eligible under the entity's authority matrix (``payroll.deferred_sale``); four eyes as the approval: never the
    preparer, the last modifier or the employee."""
    import hmac

    from django.core.exceptions import PermissionDenied

    from core import access
    from core.models import Entity
    from hr.models import Employee

    from . import approvals
    from .models import DeferredSaleRebate

    refused = ["A bai' ajel rebate credit note is issued only by payroll's approval of that rebate, by its approver, "
               "at its amount (R128). / يصدر الإشعار الدائن لإبراء البيع الآجل فقط من اعتماد الرواتب لذلك الإبراء، من "
               "معتمده، وبمبلغه."]
    marker = _rebating.get()
    match = _REBATE_REF.match(str(source_ref or ""))
    approver_pk = getattr(approver, "pk", None)
    if marker is None or match is None or marker[1] != approver_pk or int(match.group(1)) != marker[0] \
            or len(marker[2]) < 32:
        return refused
    r = DeferredSaleRebate.objects.filter(pk=marker[0], contract_id=contract.pk,
                                          entity_id=contract.entity_id).first()  # read again (the approval locks it)
    makers_refused = approver_pk in {r.created_by_id, r.modified_by_id} if r is not None else True
    if makers_refused and getattr(marker[3], "top_authority", False):  # access R79 / payroll R84: as the approval
        makers_refused = False  # (the grant's log row says top_authority; verify_grant below checks it, R31)
    if r is None or r.status not in (DeferredSaleRebate.Status.DRAFT, DeferredSaleRebate.Status.REJECTED) \
            or r.amount != amount or makers_refused \
            or not hmac.compare_digest(r.approval_token or "", marker[2]):
        return refused
    emp_user = Employee.objects.filter(pk=contract.employee_id).values_list("user_id", flat=True).first()
    if emp_user and emp_user == approver_pk:
        return refused
    grant = marker[3]
    entity = Entity.objects.get(pk=contract.entity_id)
    try:
        if getattr(getattr(grant, "user", None), "pk", None) != approver_pk:
            return refused
        access.verify_grant(grant, entity, ("payroll.approve_deferred_sale",), document=contract, use=False)
        approvals.check(grant, entity, "payroll.deferred_sale")
    except PermissionDenied:
        return refused
    return []
