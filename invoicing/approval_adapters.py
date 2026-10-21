"""Invoicing's adapters to the one approval engine (access.md §10.5, R61). Registered in ``InvoicingConfig.ready``.

Each adapter tells the engine a document's activity, amount and currency (the band measure, R73), version (so a
changed document is approved again), the people it benefits (R82) and the module services the engine inbox calls.
The approval services themselves keep every invoicing rule; they reach the engine through ``core.access.require``.
"""


def _employee_users(*parties):
    """R82: a customer or vendor record linked to an employee benefits that employee's user."""
    out = set()
    for party in parties:
        employee = getattr(party, "employee", None) if party is not None else None
        user_id = getattr(employee, "user_id", None) if employee is not None else None
        if user_id:
            out.add(user_id)
    return out


def _charity_version(recipient):
    """Inbox version of a charity recipient (web.md R7): its creation and its last (re)activation request."""
    from core.access.engine import version_text

    return f"{version_text(recipient.created_at)}|{version_text(recipient.reactivation_requested_at or '')}"[:64]


def _label(doc, en, ar):
    number = getattr(doc, "number", "") or f"#{doc.pk}"
    return f"{en} {number}", f"{ar} {number}"


def register():
    from core.access import engine

    from . import services
    from .models import CharityRecipient, PurchaseDocument, PurificationTransfer, SalesDocument, VendorPayment

    engine.register(
        SalesDocument, "invoicing.sales_document", permission="invoicing.approve_sales_document",
        submit_permission="invoicing.submit_sales_document",
        amount=services.approval_amount, currency=lambda d: d.currency_id,
        beneficiaries=lambda d: _employee_users(d.customer),
        label=lambda d: _label(d, "Sales document", "مستند مبيعات"),
        approve=lambda d, user: services.approve_sales_document(d, user=user),
        return_=lambda d, user, note: services.return_to_draft(d, user=user),
    )
    engine.register(
        PurchaseDocument, "invoicing.bill", permission="invoicing.approve_bill",
        submit_permission="invoicing.submit_bill",
        # Before approval the bill's own totals are not yet computed; the vendor document's total is the measure.
        amount=lambda d: d.vendor_total_gross if d.vendor_total_gross else d.total_gross,
        currency=lambda d: d.currency_id,
        beneficiaries=lambda d: _employee_users(d.vendor),
        label=lambda d: _label(d, "Vendor bill", "فاتورة مورد"),
        approve=lambda d, user: services.approve_purchase_document(d, user=user),
    )
    engine.register(
        VendorPayment, "invoicing.vendor_payment", permission="invoicing.approve_vendor_payment",
        submit_permission="invoicing.submit_vendor_payment",
        amount=lambda d: d.amount, currency=lambda d: d.currency_id,
        beneficiaries=lambda d: _employee_users(d.vendor),
        label=lambda d: _label(d, "Vendor payment", "دفعة مورد"),
        approve=lambda d, user: services.approve_vendor_payment(d, user=user),
    )
    for activity, permission in (("invoicing.purification_funding", "invoicing.approve_purification_funding"),
                                 ("invoicing.purification_payout", "invoicing.approve_purification_payout")):
        engine.register(
            PurificationTransfer, activity, permission=permission, amount=lambda d: d.amount,
            currency=lambda d: d.entity.base_currency_id,
            label=lambda d: _label(d, "Purification transfer", "تحويل تطهير"),
            approve=lambda d, user: services.post_purification_transfer(d, user=user),
        )
    engine.register(
        CharityRecipient, "invoicing.charity_recipient", permission="invoicing.approve_charity_recipient",
        version=lambda d: _charity_version(d),
        amount=lambda d: None, label=lambda d: (f"Charity recipient #{d.pk}", f"جهة خيرية رقم {d.pk}"),
    )
