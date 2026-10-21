"""Payroll's adapters to the one approval engine (access.md §10.5; payroll.md §11 R82 to R84). Registered in
``PayrollConfig.ready``.

Each adapter supplies the activity, the amount basis in the functional currency (payroll R82 table, OQ-54), the
employee who benefits (access R82: never approved by that employee, whatever their role) and a label with the
document number and period only (no names, no individual pay; payroll R36). Payroll's own checks stay in
``payroll.services`` and run on every decision (fingerprints, own inputs, the 3 x BASIC loan cap). Payroll never
passes ``allow_self_approval``: only the engine's top-authority path lets the entity's top authority approve a payroll
document they made (access R79, payroll R84). Slice 3: off-cycle runs reuse the ``PayrollRun`` adapter; Mudad
justifications and bai' ajel closures after exit have their own (merged 2026-10-21). Liability reports approve
nothing (generated and kept read-only).
"""


def _employee_user(record):
    employee = getattr(record, "employee", None)
    user_id = getattr(employee, "user_id", None)
    return {user_id} if user_id else set()


def _label(en, ar):
    def label(doc):
        ref = getattr(doc, "number", "") or f"#{doc.pk}"
        period = getattr(doc, "period", "") or ""
        return f"{en} {ref} {period}".strip(), f"{ar} {ref} {period}".strip()
    return label


def register():
    from core.access import engine

    from . import services
    from .models import (DeferredSaleContract, EmployeeCostItem, EmployeeLoan, FinalSettlement, GosiFine,
                         LoanWriteOff, PayrollInput, PayrollPayment, PayrollRun, SecondmentCostReport,
                         SettlementCorrection)

    IK = PayrollInput.Kind

    def approve_input(inp, user):
        if inp.kind == IK.OVERTIME_HOURS:
            return services.approve_overtime(inp, user=user)
        if inp.kind == IK.MATERNITY_SUPPORT:
            return services.approve_maternity_support(inp, user=user)
        return services.approve_input(inp, user=user)

    none = lambda d: None  # noqa: E731  (amount basis "none", payroll R82 table)
    for activity, permission in (("payroll.input", "payroll.approve_input"),
                                 ("payroll.overtime", "payroll.approve_overtime"),
                                 ("payroll.maternity_clearance", "payroll.approve_maternity_clearance")):
        engine.register(PayrollInput, activity, permission=permission, amount=none, beneficiaries=_employee_user,
                        label=_label("Payroll input", "مدخل رواتب"), approve=approve_input,
                        reject=lambda d, user, note: services.reject_input(d, user=user, reason_ar=note or "مرفوض"))
    engine.register(PayrollRun, "payroll.run", permission="payroll.approve_run",
                    submit_permission="payroll.submit_run", amount=none,
                    beneficiaries=services._run_beneficiaries,  # access R82, payroll R39 (review of 92c35c2, item 5)
                    version=lambda r: r.stored_hash or r.inputs_hash or "",
                    label=_label("Payroll run", "مسير رواتب"),
                    approve=lambda r, user: services.approve_run(r, user=user),
                    return_=lambda r, user, note: services.return_run(r, user=user, reason_ar=note or "أعيد للاحتساب"))
    engine.register(EmployeeLoan, "payroll.loan", permission="payroll.approve_loan",
                    submit_permission="payroll.create_loan", amount=lambda d: d.principal,
                    currency=lambda d: d.entity.base_currency_id, beneficiaries=_employee_user,
                    label=_label("Employee loan", "قرض موظف"),
                    approve=lambda d, user: services.approve_loan(d, user=user))
    def terms_version(c):  # review of 92c35c2, item 7: the terms decided are the terms submitted (content hash)
        from core.access.authority_models import canonical_hash

        return "terms:" + canonical_hash([c.employee_id, c.asset_description_en, c.asset_description_ar, c.asset_type,
                                          c.term_months, str(c.cost), str(c.profit_amount), str(c.vat_amount),
                                          c.first_period, c.consent_ref, c.final_dues_undertaking])[:40]

    engine.register(DeferredSaleContract, "payroll.deferred_sale_terms", permission="payroll.approve_deferred_sale_terms",
                    submit_permission="payroll.create_deferred_sale",
                    amount=none, beneficiaries=_employee_user, version=terms_version,
                    label=_label("Bai' ajel contract", "عقد بيع آجل"),
                    approve=lambda d, user: services.approve_deferred_sale_terms(d, user=user))
    from . import approvals

    def sale_or_rebate_amount(c):
        r = approvals.rebate_in_approval(c)
        return r.amount if r is not None else (c.cost or 0) + (c.profit_amount or 0)

    def sale_or_rebate_version(c):  # an ibra' rebate is its own request on the contract (10.5)
        r = approvals.rebate_in_approval(c)
        return f"rebate:{r.pk}" if r is not None else "sale"

    def rebate_makers(c):
        r = approvals.rebate_in_approval(c)
        return {r.created_by_id, r.modified_by_id} if r is not None else set()

    engine.register(DeferredSaleContract, "payroll.deferred_sale", permission="payroll.approve_deferred_sale",
                    amount=sale_or_rebate_amount, currency=lambda d: d.entity.base_currency_id,
                    version=sale_or_rebate_version, people=rebate_makers, beneficiaries=_employee_user,
                    label=_label("Bai' ajel sale or ibra' rebate", "بيع آجل أو إبراء"),
                    approve=lambda d, user: services.approve_deferred_sale(d, user=user))
    engine.register(FinalSettlement, "payroll.final_settlement", permission="payroll.approve_final_settlement",
                    submit_permission="payroll.prepare_final_settlement",
                    amount=lambda d: d.net, currency=lambda d: d.entity.base_currency_id, beneficiaries=_employee_user,
                    version=lambda d: d.stored_hash or d.inputs_hash or "", people=lambda d: {d.calculated_by_id},
                    label=_label("Final settlement", "مخالصة نهائية"),
                    approve=lambda d, user: services.approve_final_settlement(d, user=user),
                    return_=lambda d, user, note: services.return_final_settlement(d, user=user,
                                                                                   reason_ar=note or "أعيدت"))
    engine.register(SettlementCorrection, "payroll.settlement_correction",
                    permission="payroll.approve_settlement_correction",
                    submit_permission="payroll.prepare_settlement_correction", amount=lambda d: abs(d.net_change),
                    currency=lambda d: d.entity.base_currency_id, beneficiaries=_employee_user,
                    version=lambda d: d.stored_hash or "", label=_label("Settlement correction", "تصحيح مخالصة"),
                    approve=lambda d, user: services.approve_settlement_correction(d, user=user),
                    return_=lambda d, user, note: services.return_settlement_correction(d, user=user,
                                                                                        reason_ar=note or "أعيد"))
    engine.register(LoanWriteOff, "payroll.loan_write_off", permission="payroll.approve_loan_write_off",
                    submit_permission="payroll.prepare_loan_write_off",
                    amount=lambda d: d.amount, currency=lambda d: d.entity.base_currency_id,
                    beneficiaries=_employee_user, label=_label("Loan write-off", "شطب قرض"),
                    approve=lambda d, user: services.approve_loan_write_off(d, user=user),
                    reject=lambda d, user, note: services.reject_loan_write_off(d, user=user, reason_ar=note or "مرفوض"))
    def payment_beneficiaries(p):  # access R82: a final-settlement payment benefits its employee. A net-salaries
        # payment pays what the approved run fixed; its payees are not counted (the run's R82 check did that).
        fs = p.final_settlement if p.final_settlement_id else None
        return _employee_user(fs) if fs is not None else set()

    engine.register(PayrollPayment, "payroll.payment", permission="payroll.approve_payment",
                    submit_permission="payroll.record_payment", amount=lambda d: d.amount,
                    currency=lambda d: d.currency_id, beneficiaries=payment_beneficiaries,
                    label=_label("Payroll payment", "دفعة رواتب"),
                    approve=lambda d, user: services.approve_payment(d, user=user))
    engine.register(GosiFine, "payroll.gosi_fine", permission="payroll.approve_gosi_fine",
                    submit_permission="payroll.record_gosi_fine", amount=lambda d: d.amount,
                    currency=lambda d: d.entity.base_currency_id, label=_label("GOSI fine", "غرامة تأمينات"),
                    approve=lambda d, user: services.approve_gosi_fine(d, user=user))
    engine.register(EmployeeCostItem, "payroll.cost_item", permission="payroll.approve_cost_item",
                    amount=lambda d: d.amount, currency=lambda d: d.entity.base_currency_id,
                    beneficiaries=_employee_user, label=_label("Employee cost item", "بند تكلفة موظف"),
                    approve=lambda d, user: services.approve_cost_item(d, user=user),
                    reject=lambda d, user, note: services.reject_cost_item(d, user=user))
    engine.register(SecondmentCostReport, "payroll.secondment_cost_report", permission="payroll.approve_cost_report",
                    submit_permission="payroll.prepare_cost_report",
                    amount=none, label=_label("Secondment cost report", "تقرير تكلفة إعارة"),
                    approve=lambda d, user: services.approve_cost_report(d, user=user),
                    return_=lambda d, user, note: services.return_cost_report(d, user=user))
    # The maternity case (GOSI) clearance needs the legal clearance reference: decided directly, not from the inbox.
    from .models import MaternityGosiCase

    engine.register(MaternityGosiCase, "payroll.maternity_clearance", permission="payroll.approve_maternity_clearance",
                    amount=none, beneficiaries=_employee_user, label=_label("Maternity case", "حالة أمومة"))
    # Payroll slice 3 (merged 2026-10-21): Mudad justifications (activity payroll.mudad_justification, code default
    # hr_officer, the employee as beneficiary) and bai' ajel write-offs / recoveries after exit (their own activity
    # payroll.deferred_sale_closure, defaulted like the loan write-off).
    from .models import DeferredSaleClosure, MudadJustification

    engine.register(MudadJustification, "payroll.mudad_justification", permission="payroll.approve_mudad_justification",
                    amount=none, beneficiaries=_employee_user, label=_label("Mudad justification", "مبرر مدد"),
                    approve=lambda d, user: services.approve_mudad_justification(d, user=user))
    engine.register(DeferredSaleClosure, "payroll.deferred_sale_closure",
                    permission="payroll.approve_deferred_sale_closure",
                    submit_permission="payroll.prepare_loan_write_off", amount=lambda d: d.amount,
                    currency=lambda d: d.entity.base_currency_id, beneficiaries=_employee_user,
                    label=_label("Bai' ajel closure after exit", "إقفال بيع آجل بعد انتهاء الخدمة"),
                    approve=lambda d, user: services.approve_deferred_sale_closure(d, user=user),
                    reject=lambda d, user, note: services.reject_deferred_sale_closure(d, user=user,
                                                                                        reason_ar=note or "مرفوض"))
