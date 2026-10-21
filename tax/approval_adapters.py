"""Tax's adapters to the one approval engine (access.md §10.5, R61). Registered in ``TaxConfig.ready``.

VAT returns keep three different people (G3, tax R8): the tax module enforces preparer, reviewer and approver; the
engine adds that no VAT-return or tax-payment permission is ever self-approved, also by the top authority (NSA,
R80 a and b)."""


def register():
    from core.access import engine

    from . import services
    from .models import ImportVatRecord, TaxPayment, TaxRefund, TaxSettings, VatReturn

    engine.register(VatReturn, "tax.vat_return", permission="tax.approve_return", amount=lambda d: None,
                    label=lambda d: (f"VAT return #{d.pk}", f"إقرار ضريبة القيمة المضافة رقم {d.pk}"),
                    approve=lambda d, user: services.approve_return(d, user=user),
                    return_=lambda d, user, note: services.return_to_draft(d, user=user, reason_ar=note or "أعيد"))
    for model, en, ar in ((TaxPayment, "Tax payment", "دفعة ضريبية"), (TaxRefund, "Tax refund", "استرداد ضريبي"),
                          (ImportVatRecord, "Import VAT record", "سجل ضريبة استيراد")):
        engine.register(model, "tax.tax_document", permission="tax.approve_payment",
                        amount=lambda d: getattr(d, "amount_sar", None) or getattr(d, "vat_paid_sar", None),
                        currency=lambda d: d.entity.base_currency_id,
                        label=lambda d, en=en, ar=ar: (f"{en} #{d.pk}", f"{ar} رقم {d.pk}"),
                        approve=lambda d, user: services.approve_tax_document(d, user=user))
    engine.register(TaxSettings, "tax.settings", permission="tax.change_settings", amount=lambda d: None,
                    label=lambda d: (f"Tax settings of {d.entity.code}", f"إعدادات الضرائب للكيان {d.entity.code}"))
