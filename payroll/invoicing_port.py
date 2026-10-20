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
                        "payroll.finalsettlement": "payroll.approve_final_settlement"}
_REF = re.compile(r"^payroll\.deferredsaleinstallment:(\d+)$")


@contextmanager
def collecting(document, user, mode="collect"):
    """Set by ``payroll.services`` around the effects of ``approve_run`` / ``approve_final_settlement`` (``collect``)
    and around ``reverse_run``'s profit reversals (``reverse``)."""
    token = _collecting.set((document._meta.label_lower, document.pk, getattr(user, "pk", None), mode))
    try:
        yield
    finally:
        _collecting.reset(token)


def collection_problems(contract, *, source_ref, amount=None, user, mode="collect"):
    """Why invoicing may not recognise (``collect``) or reverse (``reverse``) the bai' ajel profit of ``source_ref``
    for ``contract`` now: [] when the call comes from the payroll approval that collected it, ``source_ref`` names a
    deducted (run) or settled (final settlement) installment of that very contract and ``amount`` is what was
    collected on it. Returns (problems, permission the user must hold)."""
    from .models import DeferredSaleInstallment, FinalSettlement

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
    else:
        fs = FinalSettlement.objects.filter(pk=doc_pk).first()
        ok = (mode == "collect" and fs is not None and fs.employee_id == contract.employee_id
              and inst.status == DeferredSaleInstallment.Status.SETTLED_IN_FINAL)
    if not ok or (amount is not None and inst.deducted_amount != amount):
        return wrong, permission
    return [], permission


# --- review 2026-10-20b, finding 1 (invoicing R128 as amended): a rebate credit note only from payroll's approval ----

# (rebate pk, user pk) while ``approve_deferred_sale_rebate`` asks invoicing for the rebate's credit note. Invoicing's
# ``create_deferred_sale_adjustment`` is refused outside it (``rebate_problems``).
_rebating = ContextVar("payroll_rebating", default=None)
_REBATE_REF = re.compile(r"^payroll\.deferredsalerebate:(\d+)$")


@contextmanager
def rebating(rebate, user):
    """Set by ``payroll.services.approve_deferred_sale_rebate`` around its call to ``create_bai_ajel_rebate``."""
    token = _rebating.set((rebate.pk, getattr(user, "pk", None)))
    try:
        yield
    finally:
        _rebating.reset(token)


def rebate_problems(contract, *, source_ref, amount, approver):
    """Why invoicing may not issue the rebate credit note ``source_ref`` of ``amount`` for ``contract`` now: [] only
    inside the payroll approval of that very rebate (``rebating``), by its approver (four eyes: never the preparer or
    the employee), for a rebate of that contract and entity, being approved (draft, or rejected and approved again),
    at that amount."""
    from hr.models import Employee

    from .models import DeferredSaleRebate

    refused = ["A bai' ajel rebate credit note is issued only by payroll's approval of that rebate, by its approver, "
               "at its amount (R128). / يصدر الإشعار الدائن لإبراء البيع الآجل فقط من اعتماد الرواتب لذلك الإبراء، من "
               "معتمده، وبمبلغه."]
    marker = _rebating.get()
    match = _REBATE_REF.match(str(source_ref or ""))
    approver_pk = getattr(approver, "pk", None)
    if marker is None or match is None or marker[1] != approver_pk or int(match.group(1)) != marker[0]:
        return refused
    r = DeferredSaleRebate.objects.filter(pk=marker[0], contract_id=contract.pk, entity_id=contract.entity_id).first()
    if r is None or r.status not in (DeferredSaleRebate.Status.DRAFT, DeferredSaleRebate.Status.REJECTED) \
            or r.amount != amount or approver_pk == r.created_by_id:
        return refused
    emp_user = Employee.objects.filter(pk=contract.employee_id).values_list("user_id", flat=True).first()
    if emp_user and emp_user == approver_pk:
        return refused
    return []
