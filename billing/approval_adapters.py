"""Billing's adapters to the one approval engine (access.md §10.5, R61). Registered in ``BillingConfig.ready``."""


def _contract_people(contract):
    """R14 (billing R8): the makers of version 1 count as makers of the contract."""
    v1 = contract.versions.filter(version_no=1).first()
    return {v1.created_by_id, v1.modified_by_id} if v1 is not None else set()


def _total(contract):
    """OQ-35 default: a contract is measured on its version's total value (the version's lines over the term)."""
    return getattr(contract, "total_value", None)


def register():
    from core.access import engine

    from . import services
    from .models import Contract, ContractVersion

    engine.register(
        Contract, "billing.contract", permission="billing.approve_contract",
        submit_permission="billing.submit_contract",
        amount=_total, currency=lambda c: c.currency_id, people=_contract_people,
        label=lambda c: (f"Contract {c.number or '#' + str(c.pk)}", f"عقد {c.number or '#' + str(c.pk)}"),
        approve=lambda c, user: services.approve_contract(c, user=user),
        return_=lambda c, user, note: services.return_contract(c, user=user, reason_ar=note or "أعيد للتعديل",
                                                                  reason_en=engine.note_en()),
    )
    engine.register(
        ContractVersion, "billing.version", permission="billing.approve_version",
        submit_permission="billing.edit_version_draft",
        amount=lambda v: None, currency=lambda v: v.contract.currency_id,
        label=lambda v: (f"Contract version {v.contract.number} v{v.version_no}",
                         f"نسخة العقد {v.contract.number} رقم {v.version_no}"),
        approve=lambda v, user: services.approve_version(v, user=user),
        reject=lambda v, user, note: services.reject_version(v, user=user, reason_ar=note or "مرفوض",
                                                                reason_en=engine.note_en()),
    )
