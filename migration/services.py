"""Legacy data migration services (docs/specs/migration.md). The only way to map, validate, load and post legacy data.

Loading writes through ``ledger.services`` only: drafts with ``create_entry(..., source="import")``, posted by a
second person with ``post_entry`` and a ``ledger.post_entry`` grant per entry (R14 to R16). Every function checks
the user's right in the entity (R19). Writes happen only with ``apply=True``; without it the same checks run and the
plan is returned (R16 of the commands, dry run).
"""
import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from core import access
from core.access import engine
from core.access.models import Kind
from core.access.services import kind_of
from core.models import Entity
from ledger import control_accounts
from ledger import services as ledger
from ledger.models import Account, FiscalPeriod, JournalEntry, JournalLine

from .models import AccountMap, AccountMapRow, ImportedEntry, MigrationError
from .readers import CENT, ZERO, Problem, bi, error, read_account_map_csv, warning

FALCON = "falcon"
OPENING_KEY = "opening"
OPENING_EQUITY = "3900"
SYSTEM_NAMES = {FALCON: ("Falcon", "فالكون")}
# migration.md R7: the first digit of a Falcon code fixes the ERP account types it may map to.
CLASS_TYPES = {
    "1": {Account.Type.ASSET},
    "2": {Account.Type.LIABILITY, Account.Type.EQUITY},
    "4": {Account.Type.INCOME},
    "5": {Account.Type.EXPENSE},
}
PL_DIGITS = ("4", "5")
# invoicing R97 (policy A, decision pack D32(a)): the sub-ledger control accounts, from the one list
# (``ledger.control_accounts``, read from the chart template; 2310 and 2320 are not in the chart). Only the opening
# entry may carry them (M6), tied out to the open items loaded (R22); replayed journals never (R8). An ``is_control``
# flag on the account, when the ledger adds it, is honoured too.
CONTROL_CODES = control_accounts.SUBLEDGER_CONTROL_CODES
CUSTOMER_STATEMENT = "مستخلص عميل"


def _is_control(account):
    return account.code in CONTROL_CODES or bool(getattr(account, "is_control", False))


# --- open items loaded into the sub-ledgers (R22, R23) ---------------------------------------------------------------
# R23: the sub-ledger detail behind the opening entry's balances is loaded into its module (open receivables and
# payables into invoicing as migrated open items, end-of-service provisions into payroll, bai' ajel contracts into
# payroll and invoicing) by ``migration.subledgers``; each module's balances are registered here as a *source*:
# ``fn(entity) -> {(account code, counterparty entity code or ""): Decimal}``, the signed SAR balance (debit positive,
# so payables and provisions are negative) of what is loaded for that entity, for the account codes the source
# answers for (``codes``; the control accounts by default). The R22 posting gate ties the opening entry's lines on
# those codes to them. The real sources are registered in ``MigrationConfig.ready``; tests may add stand-ins.
_OPEN_ITEM_SOURCES = []


def register_open_items_source(fn, codes=None, *, per_source_system=False):
    """Register a sub-ledger's balances for the R22 posting gate (idempotent); ``codes``: the accounts it answers for
    (default: the control accounts 1200, 1210, 2100, 2110). ``per_source_system``: the source is called
    ``fn(entity, source_system)`` and answers for the opening entry of that legacy system only (review of aa856fa,
    item 8); otherwise ``fn(entity)`` for every opening entry of the entity."""
    if all(f is not fn for f, _, _ in _OPEN_ITEM_SOURCES):
        _OPEN_ITEM_SOURCES.append((fn, frozenset(codes or CONTROL_CODES), per_source_system))


def unregister_open_items_source(fn):
    _OPEN_ITEM_SOURCES[:] = [(f, c, p) for f, c, p in _OPEN_ITEM_SOURCES if f is not fn]


def _gated_codes():
    """The accounts the R22 gate ties out: the control accounts always, and every account a registered source
    answers for (e.g. 2500 end of service, 1240 / 1241 bai' ajel)."""
    out = set(CONTROL_CODES)
    for _fn, codes, _per in _OPEN_ITEM_SOURCES:
        out |= codes
    return frozenset(out)


def _open_item_balances(entity, source_system=FALCON):
    """What is loaded into the sub-ledgers of ``entity`` per (account code, counterparty code or ""), signed (debit
    positive); ``None`` when no sub-ledger has registered a source."""
    if not _OPEN_ITEM_SOURCES:
        return None
    out = defaultdict(lambda: ZERO)
    for fn, codes, per_source_system in _OPEN_ITEM_SOURCES:
        found = fn(entity, source_system) if per_source_system else fn(entity)
        for (code, counterparty), amount in (found or {}).items():
            if str(code) in codes:
                out[(str(code), counterparty or "")] += Decimal(amount)
    return dict(out)


def _control_balances(entry, codes=None):
    """The entry's net per (account code, counterparty code or ""), debit positive, on the control accounts (or on
    ``codes``)."""
    out = defaultdict(lambda: ZERO)
    for line in JournalLine.objects.filter(entry=entry).select_related("account", "counterparty_entity"):
        if (line.account.code in codes) if codes is not None else _is_control(line.account):
            key = (line.account.code, line.counterparty_entity.code if line.counterparty_entity_id else "")
            out[key] += line.debit - line.credit
    return dict(out)


def _label(code, counterparty):
    return f"{code} ({counterparty})" if counterparty else code


def _open_item_tie_out(entry, source_system=FALCON):
    """R22: the opening entry's balances on the gated accounts (control accounts, and the accounts of the other
    registered sub-ledgers) against what is loaded into the sub-ledgers. Returns (rows, problems); rows have account,
    counterparty, ledger, open_items, difference; problems are bilingual texts (one per difference, or one when
    nothing is loaded for the entity yet)."""
    gated = _gated_codes()
    ledger_side = _control_balances(entry, gated)
    loaded = _open_item_balances(entry.entity, source_system)
    if not loaded or not any(loaded.values()):
        if not any(ledger_side.values()):
            return [], []
        shown = ", ".join(f"{_label(*k)} {v}" for k, v in sorted(ledger_side.items()) if v)
        return [], [bi(f"The opening entry has control-account balances ({shown}) but no open items are loaded into "
                       "invoicing yet (R23); it is posted only when they tie out (R22).",
                       f"في القيد الافتتاحي أرصدة على حسابات المراقبة ({shown}) ولم تُحمَّل البنود المفتوحة في الفوترة "
                       "بعد؛ لا يُرحَّل إلا بعد مطابقتها.")]
    rows, problems = [], []
    keys = set(ledger_side) | {k for k in loaded if k[0] in gated}
    for code, cp in sorted(keys):
        book = ledger_side.get((code, cp), ZERO).quantize(CENT)
        items = loaded.get((code, cp), ZERO).quantize(CENT)
        if not book and not items:
            continue
        diff = book - items
        rows.append({"account": code, "counterparty": cp, "ledger": book, "open_items": items, "difference": diff})
        if not diff:
            continue
        if code in CONTROL_CODES:
            problems.append(bi(f"Control account {_label(code, cp)}: opening entry {book}, open items loaded {items}, "
                               f"difference {diff}.",
                               f"حساب المراقبة {_label(code, cp)}: القيد الافتتاحي {book}، البنود المفتوحة المحمّلة "
                               f"{items}، الفرق {diff}."))
        else:
            problems.append(bi(f"Sub-ledger account {_label(code, cp)}: opening entry {book}, sub-ledger loaded {items}, "
                               f"difference {diff}.",
                               f"حساب السجل الفرعي {_label(code, cp)}: القيد الافتتاحي {book}، المحمّل في السجل الفرعي "
                               f"{items}، الفرق {diff}."))
    return rows, problems


def _opening_entry_allowed(entry):
    """``ledger.restricted`` allowance: the opening entry of an import (``source="import"``, linked to its
    ``ImportedEntry`` of kind ``opening``). Replayed journals are not accepted on restricted accounts."""
    return entry.source == JournalEntry.Source.IMPORT and ImportedEntry.objects.filter(
        journal_entry_id=entry.pk, entity_id=entry.entity_id, kind=ImportedEntry.Kind.OPENING).exists()


def _fail(en, ar, code=""):
    exc = MigrationError(bi(en, ar))
    exc.code = code or None
    raise exc


def _names(source_system):
    return SYSTEM_NAMES.get(source_system, (source_system, source_system))


def _prefix(source_system):
    return source_system.upper()


# --- access ------------------------------------------------------------------------------------------------------

def _require_person(user, entity, permission, document=None, **kwargs):
    """``require`` plus R19: imports, approvals and postings are done by people, never agents or system users."""
    grant = access.require(user, entity, permission, document=document, **kwargs)
    if kind_of(user) != Kind.HUMAN:
        _fail("Legacy data is imported, approved and posted by a person only.",
              "يستورد بيانات النظام السابق ويعتمدها ويرحّلها شخص فقط.", "person_only")
    return grant


def account_maps(user, entity=None):
    """Account maps the user may see (``migration.view``), optionally of one entity (R19)."""
    qs = access.scope(AccountMap.objects.select_related("entity"), user, "migration.view")
    return qs.filter(entity=entity) if entity is not None else qs


def imported_entries(user, entity=None):
    """Imported legacy entries the user may see (``migration.view``)."""
    qs = access.scope(ImportedEntry.objects.select_related("entity", "journal_entry"), user, "migration.view")
    return qs.filter(entity=entity) if entity is not None else qs


# --- account maps (R7, R8, R12, R13) -----------------------------------------------------------------------------

def _row_problems(entity, account, legacy_code, counterparty):
    """(code, en, ar) problems that make a map row wrong whatever the data (R7, R8; rule 4 and 6)."""
    out = []
    if account.entity_id != entity.pk:
        out.append(("other_entity", f"ERP account {account.code} belongs to another entity.",
                    f"حساب النظام {account.code} يخص كيانًا آخر."))
        return out
    if not account.is_postable or not account.is_active:
        out.append(("not_postable", f"ERP account {account.code} is not open for posting (header, report-only or "
                    "inactive).", f"حساب النظام {account.code} غير مفتوح للترحيل (رئيسي أو للتقارير فقط أو غير نشط)."))
    allowed = CLASS_TYPES.get(str(legacy_code)[:1])
    if allowed is not None and account.type not in allowed:
        out.append(("class_mismatch", f"Legacy code {legacy_code} is class {legacy_code[0]}; ERP account {account.code} "
                    f"is {account.type}.", f"الكود {legacy_code} من الفئة {legacy_code[0]}؛ وحساب النظام {account.code} "
                    f"من نوع {account.type}."))
    if account.is_intercompany and counterparty is None:
        out.append(("counterparty_missing", f"ERP account {account.code} is intercompany: the row needs a counterparty "
                    "entity.", f"حساب النظام {account.code} بين شركات المجموعة: يحتاج البند إلى كيان مقابل."))
    if counterparty is not None and counterparty.pk == entity.pk:
        out.append(("counterparty_self", "The counterparty cannot be the entity itself.",
                    "لا يكون الكيان المقابل هو الكيان نفسه."))
    if counterparty is not None and not account.is_intercompany:
        out.append(("counterparty_not_intercompany", f"ERP account {account.code} is not intercompany; remove the "
                    "counterparty.", f"حساب النظام {account.code} ليس بين شركات المجموعة؛ احذف الكيان المقابل."))
    return out


def _resolve_row(entity, erp_code, counterparty):
    account = erp_code if isinstance(erp_code, Account) else Account.objects.filter(entity=entity, code=str(erp_code).strip()).first()
    if account is None:
        _fail(f"ERP account {erp_code} does not exist in {entity.code}.",
              f"حساب النظام {erp_code} غير موجود في الكيان {entity.code}.", "unknown_account")
    if counterparty not in (None, "") and not isinstance(counterparty, Entity):
        found = Entity.objects.filter(code=str(counterparty).strip()).first()
        if found is None:
            _fail(f"Counterparty entity {counterparty} does not exist.", f"الكيان المقابل {counterparty} غير موجود.",
                  "unknown_counterparty")
        counterparty = found
    return account, (counterparty or None)


def _check_row(entity, legacy_code, account, counterparty):
    legacy_code = str(legacy_code).strip()
    if not (legacy_code.isdigit() and 3 <= len(legacy_code) <= 5):
        _fail(f"Legacy code {legacy_code!r} is not 3 to 5 digits.", f"الكود {legacy_code!r} ليس من 3 إلى 5 أرقام.",
              "bad_code")
    problems = _row_problems(entity, account, legacy_code, counterparty)
    if problems:
        exc = MigrationError([bi(en, ar) for _, en, ar in problems])
        exc.code = problems[0][0]
        raise exc
    return legacy_code


def _draft_map(entity, user, source_system, note=""):
    current = AccountMap.objects.filter(entity=entity, source_system=source_system, status=AccountMap.Status.DRAFT).first()
    if current is not None:
        return current
    last = AccountMap.objects.filter(entity=entity, source_system=source_system).aggregate(v=Max("version"))["v"] or 0
    return AccountMap.objects.create(entity=entity, source_system=source_system, version=last + 1, created_by=user,
                                     note=note)


@transaction.atomic
def create_account_map(entity, *, user, rows=(), source_system=FALCON, note=""):
    """A draft map (the next version) with ``rows``: dicts with legacy_code, erp_code, legacy_name, counterparty,
    note. Refused if a draft already exists (edit it instead). Every row is checked (R7, R12); nothing is guessed."""
    _require_person(user, entity, "migration.manage_account_map")
    if AccountMap.objects.filter(entity=entity, source_system=source_system, status=AccountMap.Status.DRAFT).exists():
        _fail("A draft account map already exists; edit it or discard it.",
              "توجد مسودة خريطة حسابات؛ عدّلها أو احذفها.", "draft_exists")
    amap = _draft_map(entity, user, source_system, note)
    problems, seen = [], set()
    for spec in rows:
        n = spec.get("row") or len(seen) + len(problems) + 1
        try:
            account, counterparty = _resolve_row(entity, spec.get("erp_code", ""), spec.get("counterparty"))
            code = _check_row(entity, spec.get("legacy_code", ""), account, counterparty)
        except MigrationError as exc:
            problems += [f"{bi(f'Row {n}', f'البند {n}')}: {m}" for m in exc.messages]
            continue
        if code in seen:
            problems.append(bi(f"Row {n}: legacy code {code} appears twice.", f"البند {n}: الكود {code} مكرر."))
            continue
        seen.add(code)
        AccountMapRow.objects.create(account_map=amap, legacy_code=code, legacy_name=(spec.get("legacy_name") or "")[:200],
                                     account=account, counterparty=counterparty, note=(spec.get("note") or "")[:300])
    if problems:
        raise MigrationError(problems)
    return amap


def import_account_map(entity, source, *, user, source_system=FALCON, note=""):
    """A draft map from a ``docs/migration/<entity>-account-map.csv`` file (R12)."""
    _require_person(user, entity, "migration.manage_account_map")
    return create_account_map(entity, user=user, rows=read_account_map_csv(source), source_system=source_system,
                              note=note)


def _editable(account_map, user):
    amap = AccountMap.objects.select_related("entity").get(pk=account_map.pk)
    _require_person(user, amap.entity, "migration.manage_account_map")
    if amap.status != AccountMap.Status.DRAFT:
        _fail("Only a draft account map can be changed; draft a new version.",
              "تُعدَّل مسودة خريطة الحسابات فقط؛ أعدّ نسخة جديدة.", "map_locked")
    return amap


def _touch(amap, user):
    """The user becomes the last modifier; a submitted map is changed, so it must be submitted again (R13)."""
    amap.modified_by, amap.modified_at = user, timezone.now()
    amap.submitted_by = amap.submitted_at = None
    amap.save(update_fields=["modified_by", "modified_at", "submitted_by", "submitted_at"])


@transaction.atomic
def set_map_row(account_map, *, user, legacy_code, erp_code, legacy_name="", counterparty=None, note=""):
    """Add or change one row of a draft map; the user becomes its last modifier (and cannot approve it, R13)."""
    amap = _editable(account_map, user)
    account, counterparty = _resolve_row(amap.entity, erp_code, counterparty)
    code = _check_row(amap.entity, legacy_code, account, counterparty)
    row, _ = AccountMapRow.objects.update_or_create(
        account_map=amap, legacy_code=code,
        defaults=dict(account=account, counterparty=counterparty, legacy_name=legacy_name[:200], note=note[:300]),
    )
    _touch(amap, user)
    return row


@transaction.atomic
def remove_map_row(account_map, *, user, legacy_code):
    amap = _editable(account_map, user)
    AccountMapRow.objects.filter(account_map=amap, legacy_code=str(legacy_code).strip()).delete()
    _touch(amap, user)


@transaction.atomic
def discard_account_map(account_map, *, user):
    """Delete a draft map (never an approved one)."""
    amap = _editable(account_map, user)
    amap.rows.all().delete()
    amap.delete()


def _map_problems(amap):
    if not amap.rows.exists():
        _fail("The account map has no rows.", "خريطة الحسابات بلا بنود.", "map_empty")
    bad = []
    for row in amap.rows.select_related("account", "counterparty"):
        bad += [bi(f"{row.legacy_code}: {en}", f"{row.legacy_code}: {ar}")
                for _, en, ar in _row_problems(amap.entity, row.account, row.legacy_code, row.counterparty)]
    if bad:
        raise MigrationError(bad)


@transaction.atomic
def submit_account_map(account_map, *, user):
    """R13 as amended (M7): the preparer submits a draft map for approval. Its approval request opens now in the one
    approval engine (activity ``migration.account_map``; the entity's matrix rows or the code default). A change to
    the map afterwards clears the submission; the map is then submitted again (a new request)."""
    amap = _editable(account_map, user)
    _map_problems(amap)
    amap = AccountMap.objects.select_for_update().get(pk=amap.pk)
    amap.submitted_by, amap.submitted_at = user, timezone.now()
    amap.save(update_fields=["submitted_by", "submitted_at"])
    engine.submit(amap, user, submit_permission="migration.manage_account_map")
    return amap


def return_account_map(account_map, *, user, note=""):
    """The approver returns a submitted map to its preparer (engine inbox): it stays a draft, not submitted."""
    amap = AccountMap.objects.select_related("entity").get(pk=account_map.pk)
    if not engine.deciding():  # R13 (review of aa856fa, item 6): returned through the engine inbox only
        engine.refuse("approval_request_required", user, amap.entity, "migration.approve_account_map",
                      {"document": ["migration.accountmap", amap.pk], "action": "return"}, remaining=1)
    _require_person(user, amap.entity, "migration.approve_account_map", document=amap, purpose="return")
    with transaction.atomic():
        amap = AccountMap.objects.select_for_update().get(pk=amap.pk)
        if amap.status != AccountMap.Status.DRAFT:
            _fail("Only a draft account map can be returned.", "تُعاد مسودة خريطة الحسابات فقط.", "map_not_draft")
        amap.submitted_by = amap.submitted_at = None
        amap.save(update_fields=["submitted_by", "submitted_at"])
    return amap


def decide_account_map(account_map, *, user, approve=True, note=""):
    """The approver's decision on a submitted map, through the engine inbox (``engine.decide``, R61). Returns the
    map; it is approved once the request's last step is decided (one step under the code default)."""
    amap = AccountMap.objects.select_related("entity").get(pk=account_map.pk)
    _require_person(user, amap.entity, "migration.view")  # a person of the entity; the engine checks the approval
    engine.decide(amap, user, approve=approve, note=note, permission="migration.approve_account_map")
    return AccountMap.objects.get(pk=amap.pk)


def approve_account_map(account_map, *, user):
    """R13: approve a submitted draft map. Reached only through the approval engine (``engine.decide`` /
    :func:`decide_account_map`, R61, AT-25): a direct call is refused ``approval_request_required``. The engine applies
    the entity's matrix or the code default (``migration.approve_account_map``, A): never its creator, submitter or
    last modifier, and never self-approved (NSA, access R80 h). The previous approved version becomes superseded."""
    amap = AccountMap.objects.select_related("entity").get(pk=account_map.pk)
    if not engine.deciding():
        engine.refuse("approval_request_required", user, amap.entity, "migration.approve_account_map",
                      {"document": ["migration.accountmap", amap.pk]}, remaining=1)
    _require_person(user, amap.entity, "migration.approve_account_map", document=amap)
    with transaction.atomic():
        amap = AccountMap.objects.select_for_update().get(pk=amap.pk)
        if amap.status != AccountMap.Status.DRAFT:
            _fail("Only a draft account map can be approved.", "تُعتمد مسودة خريطة الحسابات فقط.", "map_not_draft")
        if amap.submitted_at is None:
            _fail("The account map is not submitted: its preparer submits it for approval first (R13).",
                  "لم تُقدَّم خريطة الحسابات: يقدّمها مُعدّها للاعتماد أولًا.", "map_not_submitted")
        _map_problems(amap)
        for old in AccountMap.objects.filter(entity=amap.entity, source_system=amap.source_system,
                                             status=AccountMap.Status.APPROVED):
            old.status = AccountMap.Status.SUPERSEDED
            old.save(update_fields=["status"])
        AccountMap.objects.filter(pk=amap.pk).update(status=AccountMap.Status.APPROVED, approved_by=user,
                                                     approved_at=timezone.now())
    return AccountMap.objects.get(pk=amap.pk)


def _approved_map(entity, source_system):
    return AccountMap.objects.filter(entity=entity, source_system=source_system, status=AccountMap.Status.APPROVED).first()


def _map_for_validation(entity, source_system):
    """The approved map, else the draft (validation only; reported as blocking, R13)."""
    return _approved_map(entity, source_system) or AccountMap.objects.filter(
        entity=entity, source_system=source_system, status=AccountMap.Status.DRAFT).first()


# --- the validation report (R4 to R11) ---------------------------------------------------------------------------

@dataclass
class Report:
    entity: str
    kind: str
    file_name: str = ""
    sha256: str = ""
    map_version: int | None = None
    map_status: str = ""
    problems: list = field(default_factory=list)
    account_totals: list = field(default_factory=list)  # per legacy code
    erp_totals: list = field(default_factory=list)  # per ERP account
    summary: dict = field(default_factory=dict)
    tie_out: list = field(default_factory=list)
    unmapped: list = field(default_factory=list)
    already_imported: list = field(default_factory=list)

    @property
    def errors(self):
        return [p for p in self.problems if p.severity == "error"]

    @property
    def warnings(self):
        return [p for p in self.problems if p.severity == "warning"]

    @property
    def ok(self):
        return not self.errors


def _periods(entity):
    return list(FiscalPeriod.objects.filter(entity=entity).values("start_date", "end_date", "status", "name"))


def _period_problem(periods, on, **where):
    for p in periods:
        if p["start_date"] <= on <= p["end_date"]:
            if p["status"] != FiscalPeriod.Status.OPEN:
                return error("period_closed", f"{on}: period {p['name']} is closed.", f"{on}: الفترة {p['name']} مقفلة.",
                             **where)
            return None
    return error("no_period", f"{on}: no ERP period covers this date.", f"{on}: لا توجد فترة في النظام تغطي هذا التاريخ.",
                 **where)


def _not_cents(amount):
    return amount != amount.quantize(CENT)


def _map_rows(amap):
    if amap is None:
        return {}
    return {r.legacy_code: r for r in amap.rows.select_related("account", "counterparty")}


def _mapping_problems(report, entity, amap, used, kind):
    """R7 to R9 for the codes in ``used`` ({code: {name, rows, debit, credit}}); fills unmapped and erp totals.
    ``kind``: ``opening`` (control accounts allowed, listed as info, gated by R22) or ``journal`` (control accounts
    refused per entry in :func:`_journal_report`)."""
    if amap is None:
        report.problems.append(error("no_map", "No account map exists for this entity; draft and approve one.",
                                     "لا توجد خريطة حسابات لهذا الكيان؛ أعدّ خريطة واعتمدها."))
    elif amap.status != AccountMap.Status.APPROVED:
        report.problems.append(error("map_not_approved", f"Account map v{amap.version} is not approved yet; it cannot "
                                     "be used for loading.", f"خريطة الحسابات النسخة {amap.version} غير معتمدة بعد؛ لا "
                                     "تُستخدم للتحميل."))
    if amap is not None:
        report.map_version, report.map_status = amap.version, amap.status
    rows = _map_rows(amap)
    erp = defaultdict(lambda: [ZERO, ZERO, set()])
    control = []
    for code, t in sorted(used.items()):
        row = rows.get(code)
        has_amount = bool(t["debit"] or t["credit"])
        if row is None:
            report.unmapped.append({"code": code, "name": t["name"], "rows": t["rows"], "debit": t["debit"],
                                    "credit": t["credit"]})
            if has_amount:
                report.problems.append(error("unmapped", f"Legacy code {code} ({t['name']}) is not mapped "
                                             f"(debit {t['debit']}, credit {t['credit']}).",
                                             f"الكود {code} ({t['name']}) غير مربوط بحساب (مدين {t['debit']}، دائن "
                                             f"{t['credit']}).", account_code=code))
            else:
                report.problems.append(warning("unmapped_zero", f"Legacy code {code} ({t['name']}) is not mapped; it has "
                                               "no amount.", f"الكود {code} ({t['name']}) غير مربوط؛ ولا مبلغ له.",
                                               account_code=code))
            continue
        if has_amount:
            for key, en, ar in _row_problems(entity, row.account, code, row.counterparty):
                report.problems.append(error(key, f"{code}: {en}", f"{code}: {ar}", account_code=code))
            if _is_control(row.account):
                cp = row.counterparty.code if row.counterparty_id else ""
                control.append(f"{code}→{_label(row.account.code, cp)}")
            if row.account.sharia_prohibited and entity.sharia_enforced:
                report.problems.append(error(
                    "sharia_prohibited", f"{code} maps to {row.account.code} ({row.account.name_en}), Sharia-prohibited "
                    "in this entity: a human decision is needed (OQ-10).",
                    f"الكود {code} مربوط بالحساب {row.account.code} ({row.account.name_ar}) المحظور شرعًا في هذا الكيان: "
                    "يلزم قرار بشري.", account_code=code))
        e = erp[row.account.code]
        e[0] += t["debit"]
        e[1] += t["credit"]
        e[2].add(code)
    if control and kind == ImportedEntry.Kind.OPENING:  # R8 amended (M6, B2): allowed, tied out before posting
        shown = ", ".join(control)
        report.problems.append(Problem("control_account_open_items", "info", bi(
            f"Control accounts in the opening entry: {shown}. The entry is posted only when the open items loaded into "
            "invoicing tie out to them (R22).",
            f"حسابات المراقبة في القيد الافتتاحي: {shown}. لا يُرحَّل القيد إلا بعد مطابقة البنود المفتوحة المحمّلة "
            "في الفوترة معها.")))
    report.erp_totals = [{"erp_code": k, "debit": v[0], "credit": v[1], "net": v[0] - v[1],
                          "legacy_codes": ",".join(sorted(v[2]))} for k, v in sorted(erp.items())]
    return rows


def _entries(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r.entry_no].append(r)
    return groups


def _journal_checks(report, entity, journal, cutover, imported=frozenset()):
    rows = journal.rows
    for r in rows:
        where = dict(row=r.row, entry_no=r.entry_no, account_code=r.account_code)
        if r.debit < 0 or r.credit < 0:
            report.problems.append(error("negative", f"Row {r.row}: negative amount.", f"الصف {r.row}: مبلغ سالب.", **where))
        if r.debit and r.credit:
            report.problems.append(error("both_sides", f"Row {r.row}: debit and credit on one line.",
                                         f"الصف {r.row}: مدين ودائن في بند واحد.", **where))
        if not r.debit and not r.credit:
            report.problems.append(error("no_amount", f"Row {r.row}: the line has no amount.", f"الصف {r.row}: البند بلا مبلغ.",
                                         **where))
        if _not_cents(r.debit) or _not_cents(r.credit):
            report.problems.append(error("decimals", f"Row {r.row}: more than 2 decimals ({r.debit or r.credit}).",
                                         f"الصف {r.row}: أكثر من خانتين عشريتين ({r.debit or r.credit}).", **where))
    periods = _periods(entity)
    groups = _entries(rows)
    seqs = defaultdict(set)
    by_source = Counter()
    for no, lines in groups.items():
        where = dict(entry_no=no, row=lines[0].row)
        debit = sum((l.debit for l in lines), ZERO)
        credit = sum((l.credit for l in lines), ZERO)
        if debit != credit:
            report.problems.append(error("unbalanced", f"Entry {no}: debits {debit} vs credits {credit}.",
                                         f"القيد {no}: المدين {debit} والدائن {credit}.", **where))
        if len(lines) < 2:
            report.problems.append(error("one_line", f"Entry {no} has fewer than two lines.", f"القيد {no} أقل من بندين.",
                                         **where))
        dates = sorted({l.date for l in lines if l.date})
        if len(dates) > 1:
            report.problems.append(error("several_dates", f"Entry {no} has several dates: {dates}.",
                                         f"القيد {no} له أكثر من تاريخ: {dates}.", **where))
        on = dates[0] if dates else None
        if on is not None and no not in imported:  # an entry imported unchanged is skipped: its period may be closed
            p = _period_problem(periods, on, **where)
            if p:
                report.problems.append(p)
            if cutover is not None and on <= cutover:
                report.problems.append(error("before_cutover", f"Entry {no} ({on}) is on or before the cut-over "
                                             f"{cutover}; it is already in the opening balances.",
                                             f"القيد {no} ({on}) في تاريخ القطع {cutover} أو قبله؛ وهو ضمن الأرصدة الافتتاحية.",
                                             **where))
        if on is not None and len(no) == 8 and int(no) // 1_000_000 != on.year % 100:
            report.problems.append(warning("number_year", f"Entry {no}: number prefix {no[:2]} but dated {on}.",
                                           f"القيد {no}: بادئة الرقم {no[:2]} وتاريخه {on}.", **where))
        if len(no) == 8:
            seqs[no[:2]].add(int(no[2:]))
        by_source[lines[0].source or "-"] += 1
    for yy, numbers in sorted(seqs.items()):
        missing = sorted(set(range(min(numbers), max(numbers) + 1)) - numbers)
        if missing:
            shown = ", ".join(f"{yy}{n:06d}" for n in missing[:20]) + (" …" if len(missing) > 20 else "")
            report.problems.append(warning("numbering_gap", f"{len(missing)} entry number(s) missing in 20{yy}: {shown} "
                                           "(incomplete export?).", f"{len(missing)} رقم قيد مفقود في 20{yy}: {shown} "
                                           "(هل الملف ناقص؟).", entry_no=f"{yy}"))
    for key, n in Counter(r.key() for r in rows).items():
        if n > 1:
            report.problems.append(warning("duplicate_row", f"Entry {key[0]}: {n} identical rows on {key[2]} "
                                           f"({key[3] or key[4]}).", f"القيد {key[0]}: {n} صفوف متطابقة على الحساب "
                                           f"{key[2]} ({key[3] or key[4]}).", entry_no=key[0], account_code=key[2]))
    if by_source.get(CUSTOMER_STATEMENT):
        report.problems.append(Problem("customer_statements", "info", bi(
            f"{by_source[CUSTOMER_STATEMENT]} entries come from customer statements: they post to receivables without "
            "customer detail; open items are loaded separately (OQ-5).",
            f"{by_source[CUSTOMER_STATEMENT]} قيدًا من مستخلصات العملاء: تُرحَّل إلى الذمم دون تفصيل العملاء؛ وتُحمَّل "
            "البنود المفتوحة منفصلة.")))
    all_dates = [r.date for r in rows if r.date]
    report.summary.update({
        "rows": len(rows), "entries": len(groups), "debit": sum((r.debit for r in rows), ZERO),
        "credit": sum((r.credit for r in rows), ZERO), "date_from": min(all_dates) if all_dates else None,
        "date_to": max(all_dates) if all_dates else None, "by_source": dict(by_source),
        "cost_centres": len({r.cost_centre for r in rows if r.cost_centre}),
        "blank_rows": journal.stats.get("blank_rows", 0), "float_artifacts": journal.stats.get("float_artifacts", 0),
    })
    return groups


def _journal_totals(rows):
    used = {}
    for r in rows:
        t = used.setdefault(r.account_code, {"name": r.account_name, "rows": 0, "debit": ZERO, "credit": ZERO})
        t["rows"] += 1
        t["debit"] += r.debit
        t["credit"] += r.credit
        t["name"] = t["name"] or r.account_name
    return used


def _tb_checks(report, tb, basis=None):
    """R10. Returns the leaf rows by code."""
    counts = Counter(r.code for r in tb.rows)
    for code, n in counts.items():
        if n > 1:
            report.problems.append(error("duplicate_code", f"Trial balance code {code} appears {n} times.",
                                         f"الكود {code} مكرر {n} مرات في ميزان المراجعة.", account_code=code))
    leaves = {r.code: r for r in tb.rows if not r.is_header}
    headers = [r for r in tb.rows if r.is_header]
    for h in headers:
        children = [r for c, r in leaves.items() if c.startswith(h.code)]
        total = sum((r.closing for r in children), ZERO)
        if h.closing and total != h.closing:
            report.problems.append(warning("header_subtotal", f"Header {h.code}: closing {h.closing}, its accounts sum to "
                                           f"{total}.", f"الحساب الرئيسي {h.code}: الختامي {h.closing}، ومجموع حساباته "
                                           f"{total}.", row=h.row, account_code=h.code))
    if headers:
        report.problems.append(Problem("headers_excluded", "info", bi(
            f"{len(headers)} header row(s) excluded: {', '.join(h.code for h in headers[:20])}.",
            f"تم استبعاد {len(headers)} صف رئيسي: {', '.join(h.code for h in headers[:20])}.")))
    for r in leaves.values():
        values = [v for v in (r.opening, r.movement_debit, r.movement_credit, r.closing) if v is not None]
        if any(_not_cents(v) for v in values):
            report.problems.append(error("decimals", f"{r.code}: more than 2 decimals.", f"{r.code}: أكثر من خانتين عشريتين.",
                                         row=r.row, account_code=r.code))
        if r.opening is not None and r.movement is not None and r.opening + r.movement != r.closing:
            report.problems.append(error("row_inconsistent", f"{r.code}: opening {r.opening} + movement {r.movement} ≠ "
                                         f"closing {r.closing}.", f"{r.code}: الافتتاحي {r.opening} + الحركة {r.movement} "
                                         f"≠ الختامي {r.closing}.", row=r.row, account_code=r.code))
    sums = {"closing": sum((r.closing for r in leaves.values()), ZERO)}
    if "opening" in tb.has or {"opening_debit", "opening_credit"} & tb.has:
        sums["opening"] = sum((r.opening or ZERO for r in leaves.values()), ZERO)
    if {"movement_debit", "movement_credit"} & tb.has:
        sums["movement"] = sum((r.movement or ZERO for r in leaves.values()), ZERO)
    labels = {"closing": ("closing", "الختامية"), "opening": ("opening", "الافتتاحية"), "movement": ("movement", "الحركة")}
    for key, net in sums.items():
        if net:
            report.problems.append(error(f"tb_unbalanced_{key}", f"Trial balance {labels[key][0]} balances do not "
                                         f"balance: debit minus credit = {net}.", f"أرصدة ميزان المراجعة "
                                         f"{labels[key][1]} غير متوازنة: المدين ناقص الدائن = {net}."))
    if basis == "opening" and "opening" not in sums:
        report.problems.append(error("no_opening_column", "The trial balance has no opening balance columns.",
                                     "لا يحتوي ميزان المراجعة على أعمدة الرصيد الافتتاحي."))
    report.summary.update({"tb_rows": len(tb.rows), "tb_accounts": len(leaves), "tb_headers": len(headers),
                           **{f"tb_{k}_net": v for k, v in sums.items()}})
    return leaves


def _tie_out(report, journal_rows, tb):
    """R11: per legacy code, TB opening + journal movement = TB closing (and = TB movement when given)."""
    leaves = {r.code: r for r in tb.rows if not r.is_header}
    headers = {r.code for r in tb.rows if r.is_header}
    has_opening = "opening" in tb.has or bool({"opening_debit", "opening_credit"} & tb.has)
    if not has_opening:
        report.problems.append(error("no_opening_column", "The tie-out needs the trial balance's opening balances.",
                                     "تحتاج المطابقة إلى الأرصدة الافتتاحية في ميزان المراجعة."))
        return
    moves = defaultdict(lambda: ZERO)
    for r in journal_rows:
        moves[r.account_code] += r.debit - r.credit
    for code in sorted(set(moves) | set(leaves)):
        if code in headers:
            report.problems.append(error("posted_to_header", f"The journal posts to {code}, a header in the trial "
                                         "balance.", f"دفتر اليومية يرحّل إلى {code}، وهو حساب رئيسي في ميزان المراجعة.",
                                         account_code=code))
        tb_row = leaves.get(code)
        opening = tb_row.opening or ZERO if tb_row else ZERO
        closing = tb_row.closing if tb_row else ZERO
        expected = opening + moves[code]
        diff = closing - expected
        tb_move = tb_row.movement if tb_row else None
        line = {"code": code, "name": tb_row.name if tb_row else "", "opening": opening, "journal": moves[code],
                "expected_closing": expected, "closing": closing, "difference": diff, "tb_movement": tb_move}
        report.tie_out.append(line)
        if tb_row is None and moves[code]:
            report.problems.append(error("not_in_tb", f"{code} has journal movement {moves[code]} but is not in the "
                                         "trial balance.", f"للحساب {code} حركة {moves[code]} في دفتر اليومية وليس في "
                                         "ميزان المراجعة.", account_code=code))
        elif diff:
            report.problems.append(error("tie_out", f"{code}: opening {opening} + journal {moves[code]} = {expected}, "
                                         f"trial balance closing {closing} (difference {diff}).",
                                         f"{code}: الافتتاحي {opening} + الحركة {moves[code]} = {expected}، والختامي في "
                                         f"الميزان {closing} (الفرق {diff}).", account_code=code))
        if tb_move is not None and tb_move != moves[code]:
            report.problems.append(error("movement_difference", f"{code}: journal movement {moves[code]}, trial balance "
                                         f"movement {tb_move}.", f"{code}: الحركة في دفتر اليومية {moves[code]}، وفي "
                                         f"الميزان {tb_move}.", account_code=code))


def _journal_report(entity, journal, *, cutover, trial_balance, source_system, amap):
    report = Report(entity.code, "journal", journal.name, journal.sha256)
    report.problems += journal.problems
    rows = _map_rows(amap)
    done = dict(ImportedEntry.objects.filter(entity=entity, source_system=source_system, kind=ImportedEntry.Kind.JOURNAL,
                                             legacy_key__in=list(_entries(journal.rows))).values_list("legacy_key",
                                                                                                     "lines_hash"))
    unchanged = set()
    for no, lines in _entries(journal.rows).items():
        if no in done:
            specs = _journal_line_specs(lines, rows)
            if specs is not None and _lines_hash(specs) == done[no]:
                unchanged.add(no)
            else:
                report.problems.append(error("changed_after_import", f"Entry {no} was imported before with different "
                                             "lines (or another map).", f"القيد {no} استُورد سابقًا ببنود مختلفة (أو بخريطة "
                                             "أخرى).", entry_no=no))
    groups = _journal_checks(report, entity, journal, cutover, imported=unchanged)
    used = _journal_totals(journal.rows)
    rows = _mapping_problems(report, entity, amap, used, ImportedEntry.Kind.JOURNAL)
    for no, lines in sorted(groups.items()):  # R8 amended (M7): replayed journals never touch control accounts
        if no in unchanged:
            continue
        hits = sorted({f"{l.account_code}→{rows[l.account_code].account.code}" for l in lines
                       if l.account_code in rows and _is_control(rows[l.account_code].account)})
        if hits:
            shown = ", ".join(hits)
            report.problems.append(error(
                "control_account", f"Entry {no} ({shown}): replayed journals cannot post to control accounts; load "
                "open items through the template.",
                f"القيد {no} ({shown}): لا تُرحَّل القيود المعاد ترحيلها إلى حسابات المراقبة؛ حمّل البنود المفتوحة من "
                "خلال القالب.", entry_no=no, row=lines[0].row, account_code=hits[0].split("→")[0]))
    report.account_totals = [{"code": c, "name": t["name"], "rows": t["rows"], "debit": t["debit"],
                              "credit": t["credit"], "net": t["debit"] - t["credit"],
                              "erp_code": rows[c].account.code if c in rows else ""} for c, t in sorted(used.items())]
    if trial_balance is not None:
        _tb_checks(report, trial_balance)
        _tie_out(report, journal.rows, trial_balance)
    report.already_imported = sorted(done)
    return report, groups, rows


def validate_journal(entity, journal, *, user, trial_balance=None, cutover=None, source_system=FALCON):
    """The validation report of a parsed Falcon journal (``readers.read_falcon_journal``), with the tie-out when a
    parsed trial balance is given (R4 to R11). Writes nothing."""
    _require_person(user, entity, "migration.import")
    report, _, _ = _journal_report(entity, journal, cutover=cutover, trial_balance=trial_balance,
                                   source_system=source_system, amap=_map_for_validation(entity, source_system))
    return report


def _tb_amounts(leaves, basis):
    return {c: ((r.closing if basis == "closing" else r.opening) or ZERO) for c, r in leaves.items()}


def _tb_report(entity, tb, *, cutover, basis, source_system, amap):
    report = Report(entity.code, "trial_balance", tb.name, tb.sha256)
    report.problems += tb.problems
    leaves = _tb_checks(report, tb, basis)
    amounts = _tb_amounts(leaves, basis)
    used = {c: {"name": leaves[c].name, "rows": 1, "debit": max(a, ZERO), "credit": max(-a, ZERO)}
            for c, a in amounts.items()}
    rows = _mapping_problems(report, entity, amap, used, ImportedEntry.Kind.OPENING)
    report.account_totals = [{"code": c, "name": leaves[c].name, "rows": 1, "debit": used[c]["debit"],
                              "credit": used[c]["credit"], "net": amounts[c],
                              "erp_code": rows[c].account.code if c in rows else ""} for c in sorted(amounts)]
    if cutover is not None:
        p = _period_problem(_periods(entity), cutover)
        if p:
            report.problems.append(p)
    return report, leaves, rows


def validate_trial_balance(entity, trial_balance, *, user, cutover=None, basis="closing", source_system=FALCON):
    """The validation report of a parsed trial balance (R10, mapping R7 to R9). Writes nothing."""
    _require_person(user, entity, "migration.import")
    report, _, _ = _tb_report(entity, trial_balance, cutover=cutover, basis=basis, source_system=source_system,
                              amap=_map_for_validation(entity, source_system))
    return report


# --- loading (R14, R15) -------------------------------------------------------------------------------------------

def _q(amount):
    return str(Decimal(amount).quantize(CENT))


def _lines_hash(specs):
    items = sorted(
        (spec["account"].code, _q(spec.get("debit") or ZERO), _q(spec.get("credit") or ZERO),
         spec["counterparty"].code if spec.get("counterparty") else "", spec.get("description", ""))
        for spec in specs
    )
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()


def _entry_lines_hash(entry):
    specs = [{"account": l.account, "debit": l.debit, "credit": l.credit, "counterparty": l.counterparty_entity,
              "description": l.description}
             for l in JournalLine.objects.filter(entry=entry).select_related("account", "counterparty_entity")]
    return _lines_hash(specs)


def _side(amount):
    return {"debit": amount} if amount > 0 else {"credit": -amount}


def _opening_line_specs(leaves, amounts, rows, *, pl_to_opening_equity, entity, source_system):
    name_en, _ = _names(source_system)
    specs, pl_net = [], ZERO
    for code in sorted(amounts):
        amount = amounts[code]
        if not amount:
            continue
        if pl_to_opening_equity and code[:1] in PL_DIGITS:
            pl_net += amount
            continue
        row = rows[code]
        specs.append({"account": row.account, **_side(amount), "counterparty": row.counterparty,
                      "description": f"{name_en} {code} {leaves[code].name}"[:300]})
    net = sum((s.get("debit", ZERO) - s.get("credit", ZERO) for s in specs), ZERO)
    if net:
        equity = Account.objects.get(entity=entity, code=OPENING_EQUITY)
        label = ("P&L at cut-over / الأرباح والخسائر عند القطع" if pl_to_opening_equity
                 else "Opening balance equity / حقوق ملكية الأرصدة الافتتاحية")
        specs.append({"account": equity, **_side(-net), "description": label})
    return specs


@dataclass
class Plan:
    report: Report
    entries: list = field(default_factory=list)  # {"legacy_key", "date", "lines": n, "debit"}
    skipped: list = field(default_factory=list)
    created: list = field(default_factory=list)  # ImportedEntry rows
    applied: bool = False


def load_opening_balances(entity, trial_balance, *, user, cutover, basis="closing", pl_to_opening_equity=False,
                          apply=False, source_system=FALCON):
    """R14: one opening draft per entity and source system from a parsed trial balance, dated ``cutover``. With
    ``apply`` the draft is created (``source="import"``) when the report has no error; it is posted later by a second
    person (:func:`post_imported_entries`)."""
    _require_person(user, entity, "migration.import")
    if basis not in ("closing", "opening"):
        _fail("The balance basis is closing or opening.", "أساس الأرصدة هو الختامي أو الافتتاحي.", "bad_basis")
    access.require(user, entity, "ledger.create_entry")
    amap = _approved_map(entity, source_system)
    report, leaves, rows = _tb_report(entity, trial_balance, cutover=cutover, basis=basis, source_system=source_system,
                                      amap=amap or _map_for_validation(entity, source_system))
    plan = Plan(report)
    if report.errors:
        return plan
    specs = _opening_line_specs(leaves, _tb_amounts(leaves, basis), rows, pl_to_opening_equity=pl_to_opening_equity,
                                entity=entity, source_system=source_system)
    if len(specs) < 2:
        report.problems.append(error("nothing_to_load", "The trial balance has no balances to load.",
                                     "لا توجد أرصدة لتحميلها في ميزان المراجعة."))
        return plan
    with transaction.atomic():
        # R14: the entity's opening imports, locked so two loads never both create one; the live one decides.
        openings = list(ImportedEntry.objects.select_for_update(of=("self",)).filter(
            entity=entity, source_system=source_system, kind=ImportedEntry.Kind.OPENING).select_related(
            "journal_entry").order_by("pk"))
        live = next((imp for imp in openings if imp.superseded_at is None), None)
        reloading = live is not None and live.journal_entry.status == JournalEntry.Status.REVERSED
        if live is not None and not reloading:  # idempotent: the same balances are skipped, other balances refused
            if live.lines_hash == _lines_hash(specs):
                plan.skipped.append(live.legacy_key)
                return plan
            report.problems.append(error("already_imported", f"Other opening balances were already imported "
                                         f"({live.journal_entry.status}); discard the draft or reverse the entry first.",
                                         "تم استيراد أرصدة افتتاحية أخرى سابقًا؛ احذف المسودة أو اعكس القيد أولًا."))
            return plan
        # Team default 2026-10-23: an opening entry that was reversed is superseded by a new load, under a new key.
        key = OPENING_KEY if not openings else f"{OPENING_KEY}-{len(openings) + 1}"
        plan.entries.append({"legacy_key": key, "date": cutover, "lines": len(specs),
                             "debit": sum((s.get("debit", ZERO) for s in specs), ZERO),
                             "supersedes": live.legacy_key if reloading else None})
        if not apply:
            return plan
        if reloading:  # kept for audit; it no longer counts as the opening import (constraint one_live_opening_import)
            ImportedEntry.objects.filter(pk=live.pk).update(superseded_at=timezone.now())
        name_en, name_ar = _names(source_system)
        entry = ledger.create_entry(
            entity, cutover, f"Opening balances from {name_en} at {cutover} / الأرصدة الافتتاحية من {name_ar} في {cutover}",
            specs, user=user, reference=f"{_prefix(source_system)}-{key.upper()}", source=JournalEntry.Source.IMPORT,
        )
        plan.created.append(ImportedEntry.objects.create(
            entity=entity, source_system=source_system, kind=ImportedEntry.Kind.OPENING, legacy_key=key,
            journal_entry=entry, account_map=amap, file_sha256=trial_balance.sha256, lines_hash=_entry_lines_hash(entry),
            created_by=user,
        ))
        ledger.submit_entry(entry, user=user)  # R16 (M7): its ledger.manual_entry approval request opens now
    plan.applied = True
    return plan


def _line_description(r):
    parts = [r.description]
    if r.cost_centre:
        parts.append(f"CC/مركز التكلفة: {r.cost_centre}")
    if r.source and r.source.lower() != "journal":
        parts.append(f"{r.source} {r.source_no}".strip())
    elif r.source_no:
        parts.append(f"#{r.source_no}")
    return " | ".join(p for p in parts if p)[:300]


def _journal_line_specs(lines, rows):
    if any(l.account_code not in rows for l in lines):
        return None
    return [{"account": rows[l.account_code].account, **({"debit": l.debit} if l.debit else {"credit": l.credit}),
             "counterparty": rows[l.account_code].counterparty, "description": _line_description(l)} for l in lines]


def load_journal_entries(entity, journal, *, user, cutover=None, trial_balance=None, apply=False,
                         source_system=FALCON):
    """R15: replay each Falcon entry of a parsed journal as one draft, tagged ``FALCON-<entry number>``. Entries already
    imported are skipped; nothing is created when the report has an error; all drafts of a run are created in one
    transaction."""
    _require_person(user, entity, "migration.import")
    access.require(user, entity, "ledger.create_entry")
    amap = _approved_map(entity, source_system)
    report, groups, rows = _journal_report(entity, journal, cutover=cutover, trial_balance=trial_balance,
                                           source_system=source_system,
                                           amap=amap or _map_for_validation(entity, source_system))
    plan = Plan(report)
    done = set(report.already_imported)
    todo = []
    for no in sorted(groups):
        if no in done:
            plan.skipped.append(no)
            continue
        lines = groups[no]
        todo.append(no)
        plan.entries.append({"legacy_key": no, "date": lines[0].date, "lines": len(lines),
                             "debit": sum((l.debit for l in lines), ZERO)})
    if report.errors or not apply:
        return plan
    name_en, name_ar = _names(source_system)
    prefix = _prefix(source_system)
    try:
        with transaction.atomic():
            for no in todo:
                lines = groups[no]
                specs = _journal_line_specs(lines, rows)
                first = next((l.description for l in lines if l.description), "")
                entry = ledger.create_entry(
                    entity, lines[0].date, f"{name_en} {no} / {name_ar} {no}: {first}"[:500], specs, user=user,
                    reference=f"{prefix}-{no}", source=JournalEntry.Source.IMPORT,
                )
                plan.created.append(ImportedEntry.objects.create(
                    entity=entity, source_system=source_system, kind=ImportedEntry.Kind.JOURNAL, legacy_key=no,
                    journal_entry=entry, account_map=amap, file_sha256=journal.sha256,
                    lines_hash=_entry_lines_hash(entry), created_by=user,
                ))
                ledger.submit_entry(entry, user=user)  # R16 (M7): the approval request opens now
    except IntegrityError:  # a concurrent run imported one of these entries first (unique legacy key)
        _fail("Another import of these entries ran at the same time; nothing was created. Run again.",
              "جرى استيراد لهذه القيود في الوقت نفسه؛ لم يُنشأ شيء. أعد التشغيل.", "concurrent_import")
    plan.applied = True
    return plan


# --- posting and discarding (R16, R17) ----------------------------------------------------------------------------

def _imports(entity, source_system, kind, legacy_keys):
    qs = ImportedEntry.objects.filter(entity=entity, source_system=source_system, kind=kind).select_related(
        "journal_entry").order_by("journal_entry__date", "legacy_key")
    if legacy_keys:
        qs = qs.filter(legacy_key__in=[str(k) for k in legacy_keys])
    return list(qs)


def _import_problems(imp, entry):
    """What refuses the posting of an import draft, whoever posts it (R16, R22): lines changed since the import; for
    the opening entry the R22 tie-out of its control accounts to the open items loaded; for a replayed journal any
    control-account line (M7). Returns (problems, R22 rows); the ledger checks period, balance, Sharia and the
    poster itself."""
    problems, rows = [], None
    if _entry_lines_hash(entry) != imp.lines_hash:
        problems.append(bi(f"{imp.legacy_key}: the draft changed after the import; discard and import again.",
                           f"{imp.legacy_key}: تغيّرت المسودة بعد الاستيراد؛ احذفها واستوردها من جديد."))
    if imp.kind == ImportedEntry.Kind.OPENING:  # R22: control accounts tie out to the open items loaded
        rows, gate = _open_item_tie_out(entry, imp.source_system)
        problems += [f"{imp.legacy_key}: {p}" for p in gate]
        problems += [f"{imp.legacy_key}: {p}" for p in _bank_draft_problems(entry)]
    else:  # R16 amended (M7): a replayed draft with a control-account line is refused
        hits = sorted({code for code, _cp in _control_balances(entry)})
        if hits:
            problems.append(bi(f"{imp.legacy_key}: replayed journals cannot post to control accounts "
                               f"({', '.join(hits)}); load open items through the template.",
                               f"{imp.legacy_key}: لا تُرحَّل القيود المعاد ترحيلها إلى حسابات المراقبة "
                               f"({', '.join(hits)})؛ حمّل البنود المفتوحة من خلال القالب."))
    return problems, rows


def _bank_draft_problems(entry):
    """Review of aa856fa, item 9 (R23 banks): a bank account still a draft on a GL account the opening entry posts
    to is approved (or discarded) in banking first: its approval sets the GL account's currency, which it can no
    longer do once the opening balance is posted on it."""
    from banking.models import BankAccount

    accounts = JournalLine.objects.filter(entry=entry).values_list("account_id", flat=True)
    drafts = sorted(BankAccount.objects.filter(entity=entry.entity, status=BankAccount.Status.DRAFT,
                                               gl_account_id__in=list(accounts)).values_list("code", flat=True))
    if not drafts:
        return []
    shown = ", ".join(drafts)
    return [bi(f"Bank account draft(s) {shown} on the opening entry's accounts: approve them in banking before the "
               "opening entry posts (R23).", f"مسودات حسابات بنكية ({shown}) على حسابات القيد الافتتاحي: اعتمدها في "
               "وحدة البنوك قبل ترحيل القيد الافتتاحي.")]


def _ledger_post_check(entry):
    """Registered with ``ledger.services.register_post_check``: an import draft posts only when
    :func:`_import_problems` is empty, also when it is posted from the approval engine's inbox (R16, R22)."""
    imp = ImportedEntry.objects.filter(journal_entry_id=entry.pk).first()
    if imp is None:
        return []
    return _import_problems(imp, entry)[0]


def post_imported_entries(entity, *, user, kind, legacy_keys=None, apply=False, source_system=FALCON):
    """R16: post the import drafts of ``kind`` (``opening`` or ``journal``) by ``user`` (never the preparer). Each
    draft was submitted when it was loaded, which opened its approval request (activity ``ledger.manual_entry``); the
    posting is that request's decision through the one approval engine (``engine.decide``, R61, AT-25), and the
    ledger adapter posts it with ``ledger.services.post_entry`` and the engine's single-use grant. A draft whose lines
    changed since the import, or an opening entry that does not tie out (R22), is refused. All or nothing: one
    refusal posts none. Under a multi-step matrix a decision that is not the last is recorded and the draft waits
    (``pending``). Returns {"ready", "problems", "posted", "pending", "already_posted", "details", "open_items"}."""
    _require_person(user, entity, "migration.view")
    if not access.has(user, entity, "ledger.post_entry"):  # refused up front, also in a dry run
        access.deny(user, entity, "ledger.post_entry", "no_membership")
    result = {"ready": [], "problems": [], "posted": [], "pending": [], "already_posted": []}
    items = _imports(entity, source_system, kind, legacy_keys)
    periods = _periods(entity)
    for imp in items:
        entry = imp.journal_entry
        if entry.status != JournalEntry.Status.DRAFT:
            result["already_posted"].append(imp.legacy_key)
            continue
        problems, rows = _import_problems(imp, entry)
        if rows is not None:
            result.setdefault("open_items", {})[imp.legacy_key] = rows
        if user.pk in {entry.created_by_id, entry.submitted_by_id, entry.modified_by_id}:
            problems.insert(0, bi(f"{imp.legacy_key}: you prepared this draft; a second person posts it.",
                                  f"{imp.legacy_key}: أنت من أعدّ هذه المسودة؛ يرحّلها شخص آخر."))
        period = _period_problem(periods, entry.date)
        if period is not None:
            problems.append(f"{imp.legacy_key}: {period.message}")
        problems += [f"{imp.legacy_key}: {p}" for p in ledger.validate_entry(entry)]
        (result["problems"] if problems else result["ready"]).append(imp.legacy_key)
        result.setdefault("details", {})[imp.legacy_key] = problems
    if not apply or result["problems"] or not result["ready"]:
        return result
    ready = set(result["ready"])
    with transaction.atomic():
        for imp in items:
            if imp.legacy_key not in ready:
                continue
            outcome = engine.decide(imp.journal_entry, user, permission="ledger.post_entry")
            entry = JournalEntry.objects.get(pk=imp.journal_entry_id)
            if entry.status == JournalEntry.Status.POSTED:
                result["posted"].append(entry)
            else:  # an earlier step of a multi-step matrix: recorded, the next approver decides (R61)
                result["pending"].append(imp.legacy_key)
                result["details"][imp.legacy_key] = [bi(
                    f"{imp.legacy_key}: your approval is recorded (request {outcome.request.pk}); the next step of the "
                    "authority matrix posts it.",
                    f"{imp.legacy_key}: سُجّل اعتمادك (الطلب {outcome.request.pk})؛ تُرحّله المرحلة التالية في مصفوفة "
                    "الصلاحيات.")]
    return result


def discard_imported_drafts(entity, *, user, kind, legacy_keys=None, apply=False, source_system=FALCON):
    """R17: delete import drafts of ``kind`` and their links so they can be imported again. Posted entries are
    listed and kept (they are reversed in the ledger, never deleted)."""
    _require_person(user, entity, "migration.import")
    result = {"drafts": [], "posted": []}
    items = _imports(entity, source_system, kind, legacy_keys)
    for imp in items:
        (result["drafts"] if imp.journal_entry.status == JournalEntry.Status.DRAFT else result["posted"]).append(
            imp.legacy_key)
    if apply:
        with transaction.atomic():
            for imp in items:
                if imp.legacy_key in result["drafts"]:
                    entry = imp.journal_entry
                    imp.delete()
                    entry.delete()
    return result


# --- post-load tie-out (R18) ---------------------------------------------------------------------------------------

def tie_out_ledger(entity, trial_balance, *, user, as_of, include_drafts=True, pl_to_opening_equity=False,
                   source_system=FALCON):
    """R18: the ERP trial balance at ``as_of`` (posted entries, plus import drafts with ``include_drafts``) per ERP
    account against the mapped legacy TB closing balances. Returns (rows, problems); rows have erp_code, expected,
    actual, difference."""
    _require_person(user, entity, "migration.view")
    access.require(user, entity, "ledger.view")
    amap = _approved_map(entity, source_system)
    problems = []
    if amap is None:
        _fail("No approved account map for this entity.", "لا توجد خريطة حسابات معتمدة لهذا الكيان.", "no_map")
    rows = _map_rows(amap)
    expected = defaultdict(lambda: ZERO)
    for code, r in ((r.code, r) for r in trial_balance.rows if not r.is_header):
        if not r.closing:
            continue
        if code not in rows:
            problems.append(error("unmapped", f"Legacy code {code} is not mapped (closing {r.closing}).",
                                  f"الكود {code} غير مربوط (الختامي {r.closing}).", account_code=code))
            continue
        target = OPENING_EQUITY if pl_to_opening_equity and code[:1] in PL_DIGITS else rows[code].account.code
        expected[target] += r.closing
    booked = Q(entry__status__in=[JournalEntry.Status.POSTED, JournalEntry.Status.REVERSED])
    if include_drafts:
        booked |= Q(entry__status=JournalEntry.Status.DRAFT, entry__legacy_import__isnull=False)
    actual = {
        r["account__code"]: (r["d"] or ZERO) - (r["c"] or ZERO)
        for r in JournalLine.objects.filter(booked, entry__entity=entity, entry__date__lte=as_of)
        .values("account__code").annotate(d=Sum("debit"), c=Sum("credit"))
    }
    out = []
    for code in sorted(set(expected) | set(actual)):
        exp, act = expected.get(code, ZERO), actual.get(code, ZERO)
        if not exp and not act:
            continue
        out.append({"erp_code": code, "expected": exp, "actual": act, "difference": act - exp})
        if act != exp:
            problems.append(error("ledger_difference", f"ERP {code}: ledger {act}, legacy trial balance {exp} "
                                  f"(difference {act - exp}).", f"الحساب {code}: الدفتر {act}، وميزان النظام السابق {exp} "
                                  f"(الفرق {act - exp}).", account_code=code))
    return out, problems
