"""Thin input/output layer for /api/v1/intercompany/. Amounts are Decimal strings; text is {"en", "ar"}."""
from rest_framework import serializers

from access import services as access
from approvals.serializers import bi

from .models import IntercompanyBill, ServiceAgreement

DEC = {"max_digits": 18, "decimal_places": 2, "coerce_to_string": True}


def money(v):
    """Money and percentages as Decimal strings with 2 decimals (approvals R22)."""
    from .services import money as q

    return None if v is None else str(q(v))


def _pct(v):
    return None if v is None else str(v)


def entity_ref(e):
    return {"id": e.pk, "code": e.code, "name": bi(e.name_en, e.name_ar)}


def source(obj):
    return {"source": obj.source, "source_agent": obj.source_agent}


def agreement_out(a):
    return {
        "id": a.pk, "number": a.number, "entity": entity_ref(a.entity), "counterparty": entity_ref(a.counterparty),
        "service_type": a.service_type.code, "description": bi(a.description_en, a.description_ar),
        "pricing_method": a.pricing_method, "currency": a.currency, "hourly_rate": money(a.hourly_rate),
        "fixed_fee": money(a.fixed_fee), "markup_pct": _pct(a.markup_pct), "cost_pool": a.cost_pool_id,
        "allocation_basis": a.allocation_basis, "allocation_pct": _pct(a.allocation_pct),
        "vat_treatment": a.vat_treatment,
        "vat_exemption_reason": bi(a.vat_exemption_reason_en, a.vat_exemption_reason_ar),
        "billing_frequency": a.billing_frequency, "payment_terms_days": a.payment_terms_days,
        "start_date": a.start_date, "end_date": a.end_date, "income_account_code": a.income_account_code,
        "expense_account_code": a.expense_account_code, "tp_method": a.tp_method,
        "tp_documentation_ref": a.tp_documentation_ref, "status": a.status, "source_type": a.source_type,
        "source_ref": a.source_ref, "rates": [
            {"id": r.pk, "role": bi(r.role_en, r.role_ar), "hourly_rate": money(r.hourly_rate),
             "valid_from": r.valid_from, "valid_to": r.valid_to} for r in a.rates.order_by("id")],
        **source(a),
    }


def timesheet_out(e):
    return {
        "id": e.pk, "entity": e.entity.code, "agreement": e.agreement_id, "user": e.user.username, "rate": e.rate_id,
        "date": e.date, "hours": money(e.hours), "description": bi(e.description_en, e.description_ar),
        "status": e.status, "invoice_line": e.invoice_line_id, **source(e),
    }


def pool_out(p):
    return {"id": p.pk, "entity": p.entity.code, "code": p.code, "name": bi(p.name_en, p.name_ar),
            "accounts": sorted(p.accounts.values_list("code", flat=True)), "is_active": p.is_active, **source(p)}


def item_out(i):
    return {"id": i.pk, "entity": i.entity.code, "agreement": i.agreement_id, "period_start": i.period_start,
            "period_end": i.period_end, "description": bi(i.description_en, i.description_ar),
            "quantity": str(i.quantity), "unit_price": str(i.unit_price), "source_type": i.source_type,
            "source_ref": i.source_ref, "status": i.status, "invoice_line": i.invoice_line_id, **source(i)}


def run_out(r):
    return {"id": r.pk, "entity": r.entity.code, "period": r.period.name, "run_date": r.run_date, "status": r.status,
            "invoices_created": r.invoices_created, "log": r.log,
            "started_by": r.created_by.username if r.created_by_id else None, **source(r)}


def invoice_out(inv, user):
    bill = IntercompanyBill.objects.filter(invoice=inv).first()
    if bill is None:
        mirror = None
    elif access.has_perm(user, bill.entity, "intercompany.bill.view"):
        mirror = {"id": bill.pk, "number": bill.number, "status": bill.status}
    else:
        mirror = {"exists": True}  # R19: the receiver's coding is not shown without rights there
    return {
        "id": inv.pk, "kind": inv.kind, "number": inv.number, "entity": entity_ref(inv.entity),
        "counterparty": entity_ref(inv.counterparty), "agreement": inv.agreement.number,
        "credit_of": inv.credit_of.number if inv.credit_of_id else None,
        "credit_reason": bi(inv.credit_reason_en, inv.credit_reason_ar), "issue_date": inv.issue_date,
        "supply_date_from": inv.supply_date_from, "supply_date_to": inv.supply_date_to, "due_date": inv.due_date,
        "billing_period": inv.billing_period, "currency": inv.currency, "net_total": money(inv.net_total),
        "vat_total": money(inv.vat_total), "gross_total": money(inv.gross_total), "status": inv.status,
        "journal_entry": inv.journal_entry.number if inv.journal_entry_id else None,
        "zatca": {"ready": inv.zatca_ready, "status": inv.zatca_status, "uuid": str(inv.zatca_uuid) if inv.zatca_uuid else None,
                  "invoice_type_code": inv.invoice_type_code, "seller_vat_number": inv.seller_vat_number,
                  "buyer_vat_number": inv.buyer_vat_number},
        "mirror_bill": mirror, "billing_run": inv.billing_run_id,
        "lines": [
            {"id": l.pk, "description": bi(l.description_en, l.description_ar), "quantity": str(l.quantity),
             "unit_price": str(l.unit_price), "net_amount": money(l.net_amount), "vat_treatment": l.vat_treatment,
             "vat_category": l.vat_category, "vat_rate": money(l.vat_rate), "vat_amount": money(l.vat_amount),
             "income_account": l.income_account.code, "pricing_basis": l.pricing_basis}
            for l in inv.lines.select_related("income_account").order_by("id")],
        **source(inv),
    }


def bill_out(b):
    return {
        "id": b.pk, "kind": b.kind, "number": b.number, "entity": entity_ref(b.entity),
        "counterparty": entity_ref(b.counterparty), "invoice": b.invoice.number,
        "credit_of": b.credit_of.number if b.credit_of_id else None, "bill_date": b.bill_date, "due_date": b.due_date,
        "currency": b.currency, "net_total": money(b.net_total), "vat_total": money(b.vat_total),
        "gross_total": money(b.gross_total), "status": b.status, "dispute_reason": b.dispute_reason,
        "journal_entry": b.journal_entry.number if b.journal_entry_id else None,
        "lines": [
            {"id": l.pk, "description": bi(l.description_en, l.description_ar), "net_amount": money(l.net_amount),
             "vat_rate": money(l.vat_rate), "vat_amount": money(l.vat_amount),
             "expense_account": l.expense_account.code, "cost_centre": l.cost_centre}
            for l in b.lines.select_related("expense_account").order_by("id")],
        **source(b),
    }


def report_out(report):
    def conv(v):
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        if hasattr(v, "quantize"):
            return money(v)
        return v

    return conv(report)


# ---------------------------------------------------------------- inputs


class AgreementInput(serializers.Serializer):
    entity = serializers.CharField(required=False)
    counterparty = serializers.CharField(required=False)
    service_type = serializers.CharField(required=False)
    description_en = serializers.CharField(required=False)
    description_ar = serializers.CharField(required=False)
    pricing_method = serializers.ChoiceField(ServiceAgreement.Pricing.choices, required=False)
    hourly_rate = serializers.DecimalField(**DEC, required=False, allow_null=True)
    fixed_fee = serializers.DecimalField(**DEC, required=False, allow_null=True)
    markup_pct = serializers.DecimalField(max_digits=6, decimal_places=3, required=False, allow_null=True)
    cost_pool = serializers.IntegerField(required=False, allow_null=True)
    allocation_basis = serializers.ChoiceField(ServiceAgreement.AllocationBasis.choices, required=False,
                                               allow_blank=True)
    allocation_pct = serializers.DecimalField(max_digits=6, decimal_places=3, required=False, allow_null=True)
    vat_treatment = serializers.ChoiceField(ServiceAgreement.VatTreatment.choices, required=False)
    vat_exemption_reason_en = serializers.CharField(required=False, allow_blank=True)
    vat_exemption_reason_ar = serializers.CharField(required=False, allow_blank=True)
    billing_frequency = serializers.ChoiceField(ServiceAgreement.Frequency.choices, required=False)
    payment_terms_days = serializers.IntegerField(required=False, min_value=0)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False, allow_null=True)
    income_account_code = serializers.CharField(required=False, allow_blank=True)
    expense_account_code = serializers.CharField(required=False, allow_blank=True)
    tp_method = serializers.ChoiceField(ServiceAgreement.TpMethod.choices, required=False, allow_blank=True)
    tp_documentation_ref = serializers.CharField(required=False, allow_blank=True)
    source_type = serializers.CharField(required=False, allow_blank=True)
    source_ref = serializers.CharField(required=False, allow_blank=True)


class TimesheetInput(serializers.Serializer):
    agreement = serializers.IntegerField(required=False)
    worker = serializers.CharField(required=False, help_text="username; default: yourself")
    rate = serializers.IntegerField(required=False, allow_null=True)
    date = serializers.DateField(required=False)
    hours = serializers.DecimalField(max_digits=6, decimal_places=2, coerce_to_string=True, required=False)
    description_en = serializers.CharField(required=False)
    description_ar = serializers.CharField(required=False)


class PoolInput(serializers.Serializer):
    entity = serializers.CharField()
    code = serializers.CharField()
    name_en = serializers.CharField()
    name_ar = serializers.CharField()
    accounts = serializers.ListField(child=serializers.CharField(), required=False, default=list)


class ItemInput(serializers.Serializer):
    agreement = serializers.IntegerField()
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    description_en = serializers.CharField()
    description_ar = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, coerce_to_string=True)
    unit_price = serializers.DecimalField(max_digits=18, decimal_places=4, coerce_to_string=True)
    source_type = serializers.CharField(required=False, allow_blank=True, default="")
    source_ref = serializers.CharField(required=False, allow_blank=True, default="")


class CreditNoteInput(serializers.Serializer):
    reason_en = serializers.CharField()
    reason_ar = serializers.CharField()
    issue_date = serializers.DateField(required=False)
    lines = serializers.ListField(child=serializers.DictField(), required=False, allow_null=True, default=None)
