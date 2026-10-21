"""Invoicing's side of the migration open-item loader (invoicing R115, OQ-24; migration R23, OQ-12, OQ-13).

The migration app (``migration.subledgers``) reads the workbook and calls these functions inside its own transaction;
invoicing alone writes invoicing data:

- ``ensure_customer`` / ``ensure_vendor``: the party of an open item, created through ``create_customer`` /
  ``create_vendor`` (their own rights and checks, R4, R16, R91; personal data as for any party, R49 to R53), or the
  existing party with that code. A re-run creates nothing.
- ``load_open_item``: one :class:`~invoicing.models.MigratedOpenItem`, the migration document type: a historic
  (pre-go-live) document, ``source="import"``, final as loaded, **never sent to ZATCA** and with **no journal entry**
  (its amount is in the migration opening entry). Unique per entity, party, document type and legacy number; a re-run
  with the same values is skipped, with other values refused (``open_item_changed``).
- ``open_item_balances``: the signed SAR balance (debit positive) of the loaded items per (control account,
  counterparty entity code or ""), the R22 source the migration posting gate ties the opening entry to.

Foreign-currency items (OQ-13 default): the open amount is kept in the document currency (up to the currency's own
decimals) and in SAR at the cut-over; the opening entry and the R22 tie-out use the SAR amount.
"""
from datetime import date as date_type
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Sum

from core import access
from core.access import services as access_services
from core.access.models import Kind
from core.models import Entity
from ledger import fx

from . import services
from .models import Customer, InvoicingError, MigratedOpenItem, Vendor, error

ZERO = Decimal("0")
CENT = Decimal("0.01")
Side = MigratedOpenItem.Side


def _human(user, entity, permission):
    if access_services.kind_of(user) != Kind.HUMAN:
        access.deny(user, entity, permission, "agent_forbidden")


def _entity(value):
    if value in (None, ""):
        return None
    if isinstance(value, Entity):
        return value
    found = Entity.objects.filter(code=str(value).strip().upper()).first()
    if found is None:
        raise error(f"Group entity {value} does not exist in the ERP.", f"كيان المجموعة {value} غير موجود في النظام.",
                    code="unknown_group_entity")
    return found


def _ensure(model, create, entity, user, code, fields):
    existing = model.objects.filter(entity=entity, code=code).first()
    if existing is not None:
        group = fields.get("group_entity")
        if (existing.group_entity_id or None) != (getattr(group, "pk", None)):
            raise error(f"{code} already exists with another group entity; correct the workbook or the party first.",
                        f"{code} موجود مسبقًا بكيان مجموعة آخر؛ صحّح الملف أو الطرف أولًا.", code="party_group_differs")
        return existing, False
    return create(entity, user=user, code=code, **fields), True


def ensure_customer(entity, *, user, code, **fields):
    """The customer ``code`` of ``entity``: the existing one, or a new one through ``create_customer``
    (``invoicing.manage_customer``, checked in both cases). ``group_entity`` may be given as an entity code."""
    access.require(user, entity, "invoicing.manage_customer")
    fields["group_entity"] = _entity(fields.get("group_entity"))
    return _ensure(Customer, services.create_customer, entity, user, code, fields)


def ensure_vendor(entity, *, user, code, **fields):
    """The vendor ``code`` of ``entity``: the existing one, or a new one through ``create_vendor``
    (``invoicing.manage_vendor``, checked in both cases; withholding-tax fields need ``invoicing.enter_wht_data``)."""
    access.require(user, entity, "invoicing.manage_vendor")
    fields["group_entity"] = _entity(fields.get("group_entity"))
    return _ensure(Vendor, services.create_vendor, entity, user, code, fields)


def control_of(party, side):
    """(control account, counterparty entity or None) an open item of ``party`` sits on (R39, migration W8)."""
    if side == Side.RECEIVABLE:
        return ("1210", party.group_entity) if party.group_entity_id else ("1200", None)
    return ("2110", party.group_entity) if party.group_entity_id else ("2100", None)


def _check(entity, side, party, doc_type, currency, amount, amount_sar, document_date, due_date, cutover):
    problems = []
    if party.entity_id != entity.pk:
        problems.append("The party belongs to another entity. / الطرف يتبع كيانًا آخر.")
    if doc_type not in MigratedOpenItem.SIDE_TYPES[side]:
        problems.append(f"{doc_type} is not a {side} document type. / النوع {doc_type} لا يخص هذا الجانب.")
    if not fx.has_places(amount, currency.minor_units):
        problems.append(f"{amount} has more decimals than {currency.code} allows ({currency.minor_units}). / "
                        f"المبلغ {amount} بخانات عشرية أكثر مما تسمح به العملة {currency.code}.")
    if amount_sar != amount_sar.quantize(CENT):
        problems.append(f"The SAR amount {amount_sar} has more than 2 decimals. / المبلغ بالريال {amount_sar} بأكثر من "
                        "خانتين عشريتين.")
    if amount <= ZERO or amount_sar <= ZERO:
        problems.append("Open amounts are above zero. / المبالغ المفتوحة أكبر من صفر.")
    if currency.code == entity.base_currency_id and amount != amount_sar:
        problems.append(f"A {currency.code} item: amount {amount} must equal the SAR amount {amount_sar}. / بند بعملة "
                        f"{currency.code}: يجب أن يساوي المبلغ {amount} المبلغ بالريال {amount_sar}.")
    if not isinstance(document_date, date_type) or not isinstance(cutover, date_type):
        problems.append("The document date and the cut-over date are required. / تاريخ المستند وتاريخ التحويل مطلوبان.")
    elif document_date > cutover:
        problems.append(f"The document date {document_date} is after the cut-over {cutover}. / تاريخ المستند "
                        f"{document_date} بعد تاريخ التحويل {cutover}.")
    if due_date is not None and isinstance(document_date, date_type) and due_date < document_date:
        problems.append("The due date is before the document date. / تاريخ الاستحقاق قبل تاريخ المستند.")
    return problems


def load_open_item(entity, *, user, side, party, doc_type, legacy_number, document_date, currency, amount, amount_sar,
                   cutover, due_date=None, description="", source_system="falcon", file_sha256=""):
    """Load one migrated open item (R115, migration R23). ``user`` holds ``migration.import`` in ``entity`` (a person,
    never an agent or system user). Returns (item, created). Nothing is sent to ZATCA and nothing is posted."""
    access.require(user, entity, "migration.import")
    _human(user, entity, "migration.import")
    currency = services._currency(currency, entity)
    amount = services._decimal(amount, "amount")
    amount_sar = services._decimal(amount_sar, "SAR amount")
    legacy_number = str(legacy_number or "").strip()[:40]
    if not legacy_number:
        raise error("The legacy document number is required.", "رقم المستند في النظام السابق مطلوب.",
                    code="legacy_number")
    problems = _check(entity, side, party, doc_type, currency, amount, amount_sar, document_date, due_date, cutover)
    if problems:
        raise services._problems_error(problems, "open_item_invalid")
    control, counterparty = control_of(party, side)
    party_field = "customer" if side == Side.RECEIVABLE else "vendor"
    existing = MigratedOpenItem.objects.filter(entity=entity, doc_type=doc_type, legacy_number=legacy_number,
                                               **{party_field: party}).first()
    if existing is not None:
        same = (existing.amount == amount and existing.amount_sar == amount_sar and existing.currency_id == currency.code
                and existing.document_date == document_date and existing.due_date == due_date
                and existing.control_account == control and existing.counterparty_id == getattr(counterparty, "pk", None))
        if not same:
            raise error(f"Open item {legacy_number} of {party.code} was loaded before with other values; it is never "
                        "changed (R115).", f"البند المفتوح {legacy_number} للطرف {party.code} حُمّل سابقًا بقيم أخرى؛ "
                        "لا يُعدَّل.", code="open_item_changed")
        return existing, False
    item = MigratedOpenItem(
        entity=entity, side=side, doc_type=doc_type, legacy_number=legacy_number, document_date=document_date,
        due_date=due_date, cutover_date=cutover, currency=currency, amount=amount, amount_sar=amount_sar.quantize(CENT),
        control_account=control, counterparty=counterparty, description=(description or "")[:300],
        source_system=source_system, file_sha256=file_sha256 or "", created_by=user, **{party_field: party},
    )
    item._service_write = True
    try:
        with transaction.atomic():
            item.save()
    except IntegrityError:  # a concurrent load of the same item
        raise error("This open item was loaded at the same time by another run; run again.",
                    "حُمّل هذا البند في الوقت نفسه من تشغيل آخر؛ أعد التشغيل.", code="concurrent_load") from None
    return item, True


def open_item_balances(entity):
    """The R22 source: {(control account, counterparty entity code or ""): signed SAR, debit positive} of the open
    items loaded for ``entity``. Internal (no access check): the migration posting gate calls it for an entry it is
    already authorised to post."""
    out = {}
    rows = (MigratedOpenItem.objects.filter(entity=entity).values("control_account", "counterparty__code", "doc_type")
            .annotate(t=Sum("amount_sar")))
    for row in rows:
        key = (row["control_account"], row["counterparty__code"] or "")
        out[key] = out.get(key, ZERO) + MigratedOpenItem.SIGNS[row["doc_type"]] * Decimal(row["t"] or ZERO)
    return {k: v.quantize(CENT) for k, v in out.items()}


def open_items(user, entity=None):
    """Migrated open items ``user`` may see (``invoicing.view``, R3)."""
    qs = access.scope(MigratedOpenItem.objects.select_related("entity", "customer", "vendor"), user, "invoicing.view")
    return qs.filter(entity=entity) if entity is not None else qs


__all__ = ["InvoicingError", "control_of", "ensure_customer", "ensure_vendor", "load_open_item", "open_item_balances",
           "open_items"]
