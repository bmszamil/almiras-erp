"""REST API /api/v1/intercompany/ (thin: input parsing here, rules in intercompany.services).

Every list and lookup is filtered by entity access (R19); records the caller cannot see return 404.
Submitting for approval uses the approvals engine (also available as POST /api/v1/approvals/requests);
approve/reject/return go through /api/v1/approvals/requests/{id}/... like every other document.
"""
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from access import services as access
from access.errors import NotFound, RuleError
from access.models import User
from approvals import services as approvals
from approvals.serializers import RequestDetailSerializer
from approvals.views import ctx_for
from core.models import Entity
from ledger.models import Account, FiscalPeriod

from . import serializers as out
from . import services
from .models import ServiceAgreement, ServiceType


def _valid(serializer_cls, data, partial=False):
    s = serializer_cls(data=data, partial=partial)
    if not s.is_valid():
        raise RuleError("invalid_input", "Invalid input.", "مدخلات غير صالحة.", detail=s.errors)
    return dict(s.validated_data)


def _get(qs, pk):
    try:
        return qs.get(pk=pk)
    except (qs.model.DoesNotExist, ValueError, ValidationError):
        raise NotFound("not_found", "Not found.", "غير موجود.")


def _entity(user, code_or_id, perm="core.entity.view"):
    if not code_or_id:
        raise RuleError("entity_required", "entity is required.", "المنشأة مطلوبة.")
    code_or_id = str(code_or_id)
    qs = Entity.objects.filter(pk=code_or_id) if code_or_id.isdigit() else Entity.objects.filter(code=code_or_id)
    e = qs.first()
    if e is None or not access.has_perm(user, e, perm):
        raise NotFound("entity_not_found", "Entity not found.", "المنشأة غير موجودة.")
    return e


def _counterparty(code_or_id):
    code_or_id = str(code_or_id or "")
    qs = Entity.objects.filter(pk=code_or_id) if code_or_id.isdigit() else Entity.objects.filter(code=code_or_id)
    e = qs.first()
    if e is None:
        raise NotFound("entity_not_found", "Counterparty not found.", "الطرف المقابل غير موجود.")
    return e


def _period(entity, name):
    p = FiscalPeriod.objects.filter(entity=entity, name=name).first()
    if p is None:
        raise NotFound("period_not_found", "Period not found.", "الفترة غير موجودة.")
    return p


def _agreement_for(user, pk, perm):
    """Agreement referenced by a new draft: any agreement of an entity where the user holds `perm`
    (a PA or agent drafting hours or invoices need not hold intercompany.agreement.view)."""
    return _get(ServiceAgreement.objects.filter(entity__in=access.entities_with_perm(user, perm)), pk)


def _filter_entity(request, qs, field="entity"):
    if code := request.query_params.get("entity"):
        qs = qs.filter(**{field: _entity(request.user, code)})
    if st := request.query_params.get("status"):
        qs = qs.filter(status=st)
    return qs


def _submit(request, doc):
    r = approvals.submit(doc, request.user, ctx=ctx_for(request))
    return Response(RequestDetailSerializer(r, context={"request": request}).data, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- reference data


class ServiceTypeListView(APIView):
    def get(self, request):
        return Response([
            {"code": t.code, "name": out.bi(t.name_en, t.name_ar), "category": t.category,
             "income_account_code": t.income_account_code, "expense_account_code": t.expense_account_code,
             "is_active": t.is_active}
            for t in ServiceType.objects.all()
        ])


# ---------------------------------------------------------------- agreements


def _agreement_fields(data, user, entity=None):
    if "counterparty" in data:
        data["counterparty"] = _counterparty(data["counterparty"])
    if "service_type" in data:
        st = ServiceType.objects.filter(code=data["service_type"]).first()
        if st is None:
            raise NotFound("service_type_not_found", "Service type not found.", "نوع الخدمة غير موجود.")
        data["service_type"] = st
    if data.get("cost_pool") is not None:
        data["cost_pool"] = _get(services.visible_cost_pools(user), data["cost_pool"])
    return data


class AgreementListView(APIView):
    def get(self, request):
        qs = _filter_entity(request, services.visible_agreements(request.user))
        return Response([out.agreement_out(a) for a in qs[:500]])

    def post(self, request):
        data = _valid(out.AgreementInput, request.data)
        entity = _entity(request.user, data.pop("entity", None))
        data = _agreement_fields(data, request.user)
        a = services.create_agreement(request.user, entity=entity, counterparty=data.pop("counterparty", None),
                                      service_type=data.pop("service_type", None), ctx=ctx_for(request), **data)
        return Response(out.agreement_out(a), status=status.HTTP_201_CREATED)


class AgreementDetailView(APIView):
    def get(self, request, pk):
        return Response(out.agreement_out(_get(services.visible_agreements(request.user), pk)))

    def patch(self, request, pk):
        a = _get(services.visible_agreements(request.user), pk)
        data = _valid(out.AgreementInput, request.data, partial=True)
        if "entity" in data:
            raise RuleError("field_not_editable", "The issuing entity cannot change.", "لا يمكن تغيير المنشأة المُصدرة.")
        a = services.update_agreement(request.user, a, ctx=ctx_for(request), **_agreement_fields(data, request.user))
        return Response(out.agreement_out(a))


class AgreementActionView(APIView):
    action = None

    def post(self, request, pk):
        a = _get(services.visible_agreements(request.user), pk)
        ctx = ctx_for(request)
        if self.action == "submit":
            return _submit(request, a)
        if self.action == "suspend":
            a = services.suspend_agreement(request.user, a, request.data.get("reason", ""), ctx=ctx)
        elif self.action == "end":
            end = parse_date(str(request.data.get("end_date", "")))
            if end is None:
                raise RuleError("invalid_input", "end_date is required.", "تاريخ الانتهاء مطلوب.")
            a = services.end_agreement(request.user, a, end, request.data.get("reason", ""), ctx=ctx)
        return Response(out.agreement_out(a))


# ---------------------------------------------------------------- timesheets


def _timesheet_fields(request, data):
    if data.get("rate") is not None:
        from .models import AgreementRate

        data["rate"] = _get(AgreementRate.objects.all(), data["rate"])
    return data


class TimesheetListView(APIView):
    def get(self, request):
        return Response([out.timesheet_out(e) for e in _filter_entity(request, services.visible_timesheets(request.user))[:500]])

    def post(self, request):
        data = _valid(out.TimesheetInput, request.data)
        a = _agreement_for(request.user, data.pop("agreement", None), "intercompany.timesheet.create")
        worker = None
        if username := data.pop("worker", None):
            worker = User.objects.filter(username=username, is_active=True).first()
            if worker is None:
                raise NotFound("user_not_found", "User not found.", "المستخدم غير موجود.")
        e = services.create_timesheet(request.user, a, worker=worker, ctx=ctx_for(request),
                                      **_timesheet_fields(request, data))
        return Response(out.timesheet_out(e), status=status.HTTP_201_CREATED)


class TimesheetDetailView(APIView):
    def get(self, request, pk):
        return Response(out.timesheet_out(_get(services.visible_timesheets(request.user), pk)))

    def patch(self, request, pk):
        e = _get(services.visible_timesheets(request.user), pk)
        data = _valid(out.TimesheetInput, request.data, partial=True)
        if {"agreement", "worker"} & set(data):
            raise RuleError("field_not_editable", "Agreement and worker cannot change.", "لا يمكن تغيير الاتفاقية أو الموظف.")
        e = services.update_timesheet(request.user, e, ctx=ctx_for(request), **_timesheet_fields(request, data))
        return Response(out.timesheet_out(e))


class TimesheetActionView(APIView):
    action = None

    def post(self, request, pk):
        e = _get(services.visible_timesheets(request.user), pk)
        ctx = ctx_for(request)
        if self.action == "submit":
            e = services.submit_timesheet(request.user, e, ctx=ctx)
        elif self.action == "approve":
            e = services.approve_timesheet(request.user, e, ctx=ctx)
        else:
            e = services.reject_timesheet(request.user, e, request.data.get("reason", ""), ctx=ctx)
        return Response(out.timesheet_out(e))


# ---------------------------------------------------------------- cost pools and billable items


class CostPoolListView(APIView):
    def get(self, request):
        return Response([out.pool_out(p) for p in _filter_entity(request, services.visible_cost_pools(request.user))])

    def post(self, request):
        data = _valid(out.PoolInput, request.data)
        entity = _entity(request.user, data["entity"])
        accounts = []
        for code in data["accounts"]:
            acc = Account.objects.filter(entity=entity, code=code).first()
            if acc is None:
                raise NotFound("account_not_found", f"Account {code} not found.", f"الحساب {code} غير موجود.")
            accounts.append(acc)
        pool = services.create_cost_pool(request.user, entity, code=data["code"], name_en=data["name_en"],
                                         name_ar=data["name_ar"], accounts=accounts, ctx=ctx_for(request))
        return Response(out.pool_out(pool), status=status.HTTP_201_CREATED)


class CostPoolComputeView(APIView):
    def post(self, request, pk):
        pool = _get(services.visible_cost_pools(request.user), pk)
        access.require_perm(request.user, pool.entity, "intercompany.costpool.manage")
        cpp = services.compute_pool_period(pool, _period(pool.entity, request.data.get("period", "")),
                                           user=request.user, ctx=ctx_for(request))
        return Response({"pool": pool.code, "period": cpp.period.name, "amount": str(cpp.amount), "locked": cpp.locked})


class BillableItemListView(APIView):
    def get(self, request):
        return Response([out.item_out(i) for i in _filter_entity(request, services.visible_billable_items(request.user))[:500]])

    def post(self, request):
        data = _valid(out.ItemInput, request.data)
        a = _agreement_for(request.user, data.pop("agreement"), "intercompany.invoice.create")
        item = services.add_billable_item(request.user, a, ctx=ctx_for(request), **data)
        return Response(out.item_out(item), status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- billing runs and invoices


class BillingRunListView(APIView):
    def get(self, request):
        return Response([out.run_out(r) for r in _filter_entity(request, services.visible_billing_runs(request.user))[:200]])

    def post(self, request):
        entity = _entity(request.user, request.data.get("entity"))
        run = services.run_billing(request.user, entity, _period(entity, request.data.get("period", "")),
                                   ctx=ctx_for(request))
        return Response(out.run_out(run), status=status.HTTP_201_CREATED)


class InvoiceListView(APIView):
    def get(self, request):
        qs = _filter_entity(request, services.visible_invoices(request.user))
        if kind := request.query_params.get("kind"):
            qs = qs.filter(kind=kind)
        return Response([out.invoice_out(i, request.user) for i in qs[:500]])

    def post(self, request):
        """Manual draft for one agreement and period: {agreement, period}."""
        a = _agreement_for(request.user, request.data.get("agreement"), "intercompany.invoice.create")
        inv = services.create_invoice_draft(request.user, a, _period(a.entity, request.data.get("period", "")),
                                            ctx=ctx_for(request))
        return Response(out.invoice_out(inv, request.user), status=status.HTTP_201_CREATED)


class InvoiceDetailView(APIView):
    def get(self, request, pk):
        return Response(out.invoice_out(_get(services.visible_invoices(request.user), pk), request.user))

    def patch(self, request, pk):
        inv = _get(services.visible_invoices(request.user), pk)
        allowed = {"lines", "credit_reason_en", "credit_reason_ar"}
        if set(request.data) - allowed:
            raise RuleError("field_not_editable", "Only line descriptions and credit reasons can change on a draft.",
                            "يمكن تغيير وصف البنود وسبب الإشعار فقط.", detail=sorted(set(request.data) - allowed))
        lines = {}
        for row in request.data.get("lines", []) or []:
            lines[int(row["id"])] = {k: row[k] for k in ("description_en", "description_ar") if k in row}
        inv = services.update_invoice_draft(request.user, inv, lines=lines,
                                            credit_reason_en=request.data.get("credit_reason_en"),
                                            credit_reason_ar=request.data.get("credit_reason_ar"), ctx=ctx_for(request))
        return Response(out.invoice_out(inv, request.user))


class InvoiceActionView(APIView):
    action = None

    def post(self, request, pk):
        inv = _get(services.visible_invoices(request.user), pk)
        ctx = ctx_for(request)
        if self.action == "submit":
            return _submit(request, inv)
        if self.action == "cancel":
            inv = services.cancel_draft(request.user, inv, request.data.get("reason", ""), ctx=ctx)
        elif self.action == "issue":
            inv = services.issue_invoice(inv, request.user, ctx=ctx)
        elif self.action == "credit-notes":
            data = _valid(out.CreditNoteInput, request.data)
            cn = services.create_credit_note(request.user, inv, reason_en=data["reason_en"],
                                             reason_ar=data["reason_ar"], lines=data["lines"],
                                             issue_date=data.get("issue_date"), ctx=ctx)
            return Response(out.invoice_out(cn, request.user), status=status.HTTP_201_CREATED)
        return Response(out.invoice_out(inv, request.user))


# ---------------------------------------------------------------- mirror bills


class BillListView(APIView):
    def get(self, request):
        return Response([out.bill_out(b) for b in _filter_entity(request, services.visible_bills(request.user))[:500]])


class BillDetailView(APIView):
    def get(self, request, pk):
        return Response(out.bill_out(_get(services.visible_bills(request.user), pk)))

    def patch(self, request, pk):
        """Coding only (R16): {"lines": [{"id", "expense_account", "cost_centre"}]}."""
        bill = _get(services.visible_bills(request.user), pk)
        if set(request.data) - {"lines"}:
            raise RuleError("field_not_editable", "Only the expense account and cost centre can change (R16).",
                            "يمكن تغيير حساب المصروف ومركز التكلفة فقط.", detail=sorted(set(request.data) - {"lines"}))
        lines = {int(row.pop("id")): row for row in [dict(r) for r in request.data.get("lines", [])]}
        bill = services.update_bill_coding(request.user, bill, lines, ctx=ctx_for(request))
        return Response(out.bill_out(bill))


class BillActionView(APIView):
    action = None

    def post(self, request, pk):
        bill = _get(services.visible_bills(request.user), pk)
        ctx = ctx_for(request)
        if self.action == "submit":
            r = services.submit_bill(request.user, bill, ctx=ctx)
            return Response(RequestDetailSerializer(r, context={"request": request}).data, status=status.HTTP_201_CREATED)
        if self.action == "dispute":
            bill = services.dispute_bill(request.user, bill, request.data.get("reason", ""), ctx=ctx)
        else:
            bill = services.undispute_bill(request.user, bill, ctx=ctx)
        return Response(out.bill_out(bill))


# ---------------------------------------------------------------- reconciliation report


class ReconciliationView(APIView):
    def get(self, request):
        as_of = parse_date(request.query_params.get("as_of", "") or "")
        if as_of is None:
            raise RuleError("invalid_input", "as_of (YYYY-MM-DD) is required.", "التاريخ as_of مطلوب.")
        entity = None
        if code := request.query_params.get("entity"):
            entity = _entity(request.user, code, "intercompany.report.view")
        report = services.reconciliation_report(request.user, as_of, entity=entity)
        if request.query_params.get("export") == "xlsx":  # ("format" is reserved by DRF)
            lang = request.query_params.get("lang") or request.user.language
            lang = "ar" if lang == "ar" else "en"
            resp = HttpResponse(services.reconciliation_xlsx(report, lang),
                                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            resp["Content-Disposition"] = f'attachment; filename="intercompany-reconciliation-{as_of}.xlsx"'
            return resp
        return Response(out.report_out(report))

