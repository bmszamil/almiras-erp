"""Entity separation, agents, audit log, bilingual text and data-only entities (banking R1, R2, R43 to R47;
AT-24, AT-25, AT-31, AT-32), the catalogue (§7) and the database guards (R15, R35, R40, R45)."""
import ast
import re
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from django.db import DatabaseError, connection, transaction
from django.http import Http404

from banking import models as banking_models
from banking import services
from banking.conftest import IBANS, journal, make_entity, open_account, postgres_only, statement
from banking.models import (BankAccount, BankingError, BankingEvent, BankTransaction, Match, MatchPart, Reconciliation,
                            StatementLine)
from banking.testing import AT3_LINES, make_iban, mt940
from core import access
from core.access import catalogue
from core.access.models import AccessLog, Membership
from core.access.seed import ROLE_SEED
from core.access.testing import make_user, member
from ledger import documents
from ledger.models import FiscalPeriod
from ledger.services_setup import install_bank_sub_account

BANKING = Path(services.__file__).resolve().parent
JAN1, JAN31 = date(2027, 1, 1), date(2027, 1, 31)
ARABIC = re.compile(r"[؀-ۿ]")


def denied(fn, *args, code=None, **kwargs):
    before = AccessLog.objects.filter(event="permission_denied").count()
    with pytest.raises(access.AccessDenied) as exc:
        fn(*args, **kwargs)
    assert AccessLog.objects.filter(event="permission_denied").count() == before + 1
    if code:
        assert exc.value.code == code
    return exc.value


# --- §7 catalogue and seed --------------------------------------------------------------------------------------

def test_banking_permissions_flags_and_holders():
    perms = {c: p for c, p in catalogue.CATALOGUE.items() if c.startswith("banking.")}
    assert {c for c, p in perms.items() if p.agent_ok} == {"banking.view", "banking.suggest_match",
                                                           "banking.create_bank_transaction"}
    assert perms["banking.approve_bank_transaction"].is_approval and perms["banking.approve_bank_transaction"].posts
    assert "banking.approve_bank_transaction" in catalogue.DOCUMENT_POST_PERMISSIONS
    assert "banking.reverse_bank_transaction" in catalogue.REVERSE_PERMISSIONS
    assert "banking.reverse_bank_transaction" not in catalogue.DOCUMENT_POST_PERMISSIONS
    assert perms["banking.approve_bank_account"].is_segregated and perms["banking.reopen_reconciliation"].is_segregated
    roles = {code: set(spec[5]) for code, spec in ROLE_SEED.items()}
    assert {"banking.import_statement", "banking.match", "banking.prepare_reconciliation"} <= roles["accountant"]
    assert {"banking.import_statement", "banking.match"} <= roles["group_treasury"]
    assert not {p for p in roles["accountant"] if p.startswith("banking.approve")}
    assert {"banking.approve_bank_transaction", "banking.reverse_bank_transaction",
            "banking.manage_settings"} <= roles["approver"]
    assert {c for c in perms if perms[c].is_approval} <= roles["managing_partner"]
    assert {p for p in roles["viewer"] if p.startswith("banking.")} == {"banking.view"}


def test_bank_transaction_is_registered_with_its_line_hooks():
    doc_type = documents.get("banking.banktransaction")
    assert doc_type.post_permissions == {"banking.approve_bank_transaction"}
    assert doc_type.reverse_permissions == {"banking.reverse_bank_transaction"}
    assert doc_type.build_lines is services._bank_transaction_lines
    assert doc_type.validate_lines is services._check_bank_transaction_lines
    assert doc_type.pending_documents is services._submitted_bank_transactions
    assert doc_type.creator_may_post is None


# --- R1, R2 / AT-24 ----------------------------------------------------------------------------------------------

def test_at24_entity_separation(w, accounts):
    min_account = open_account(w, w.min, "MIN-SAR", "riyad", w.gl.min_bank, "SAR", "islamic", opening="0.00",
                               iban=make_iban("20", "000000000000400001"))
    sub_imp = statement(w, accounts.sub, [(date(2027, 1, 5), "C", "500.00", "NTRF", "", "", "x")], start=JAN1, end=JAN31)
    sub_line = sub_imp.lines.get()
    assert set(services.visible_bank_accounts(user=w.a)) == {accounts.riyad, accounts.rajhi, accounts.snb, accounts.kwd}
    assert set(services.bank_accounts(w.alm, user=w.a)) == {accounts.riyad, accounts.rajhi, accounts.snb, accounts.kwd}
    denied(services.bank_accounts, w.sub, user=w.a, code="no_membership")
    with pytest.raises(Http404):
        services.get_bank_account(accounts.sub.pk, user=w.a)
    denied(services.statement_lines, accounts.sub, user=w.a)
    denied(services.import_statement, accounts.sub, b"x", user=w.a)
    denied(services.manual_match, accounts.sub, user=w.a, statement_lines=[(sub_line, None)])
    denied(services.prepare_reconciliation, accounts.sub, FiscalPeriod.objects.get(entity=w.sub, name="2027-01"),
           user=w.a)
    entry = journal(w, w.alm, date(2027, 1, 5), [{"account": "1101", "debit": D("500.00")},
                                                 {"account": "4100", "credit": D("500.00")}])
    with pytest.raises(BankingError, match="another bank account"):  # R2, also for group treasury
        services.manual_match(accounts.riyad, user=w.t, statement_lines=[(sub_line, None)],
                              journal_lines=[(entry.lines.get(account__code="1101"), None)])
    with pytest.raises(BankingError, match="intercompany transfer"):
        services.create_bank_transaction(accounts.riyad, user=w.a, kind="own_transfer", amount=D("10.00"),
                                         date=date(2027, 1, 5), direction="out", to_bank_account=accounts.sub)
    with pytest.raises(BankingError, match="another entity"):
        services.create_bank_transaction(accounts.riyad, user=w.t, kind="bank_charge", statement_line=sub_line)
    visible = set(services.visible_bank_accounts(user=w.t))
    assert accounts.sub in visible and min_account not in visible  # group role: full subsidiaries only (access R6)


# --- R43 / AT-25 -------------------------------------------------------------------------------------------------

def test_at25_agents_draft_and_propose_only(w, accounts):
    statement(w, accounts.riyad, [(date(2027, 1, 20), "D", "25.00", "NCHG", "", "", "CHARGES"),
                                  (date(2027, 1, 21), "C", "900.00", "NTRF", "", "", "")], start=JAN1, end=JAN31)
    charge_line, credit = StatementLine.objects.filter(bank_account=accounts.riyad).order_by("line_no")
    rows = [services.account_row(a, user=w.g) for a in services.bank_accounts(w.alm, user=w.g)]
    assert rows and all(IBANS["RIYAD-SAR"] not in str(r) for r in rows)
    assert BankingEvent.objects.filter(actor=w.g, event="agent_read", actor_kind="agent").exists()
    denied(services.reveal_iban, accounts.riyad, user=w.g, code="agent_forbidden")
    denied(services.import_statement, accounts.riyad, b"x", user=w.g, code="agent_forbidden")
    entry = journal(w, w.alm, date(2027, 1, 21), [{"account": "1101", "debit": D("900.00")},
                                                  {"account": "4100", "credit": D("900.00")}], reference="AGT-900")
    proposal = services.propose_match(accounts.riyad, user=w.g, statement_lines=[(credit, None)],
                                      journal_lines=[(entry.lines.get(account__code="1101"), None)])
    assert proposal.status == "proposed" and proposal.proposed_by == w.g
    denied(services.confirm_match, proposal, user=w.g, code="agent_forbidden")
    txn = services.create_bank_transaction(accounts.riyad, user=w.g, kind="bank_charge", statement_line=charge_line)
    assert (txn.status, txn.source) == ("draft", "agent:bank_assistant")
    denied(services.submit_bank_transaction, txn, user=w.g, code="agent_forbidden")
    services.submit_bank_transaction(txn, user=w.a)
    denied(services.approve_bank_transaction, txn, user=w.g, code="agent_forbidden")
    denied(services.prepare_reconciliation, accounts.riyad, FiscalPeriod.objects.get(entity=w.alm, name="2027-01"),
           user=w.g, code="agent_forbidden")
    created = services.run_matching(accounts.riyad, user=w.g)  # an agent's run only proposes
    assert all(m.status == "proposed" for m in created)
    events = BankingEvent.objects.filter(actor=w.g)
    assert {"match_proposed", "bank_transaction_created", "agent_read"} <= set(events.values_list("event", flat=True))
    assert set(events.values_list("actor_kind", flat=True)) == {"agent"}


# --- R45 / AT-31 -------------------------------------------------------------------------------------------------

def test_at31_one_event_per_step_and_the_log_is_append_only(w, accounts):
    from banking.conftest import receipt

    receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    imp = services.import_statement(accounts.riyad, mt940(
        statement_id="R1", sequence=1, currency="SAR", opening="100000.00", opening_date=JAN1, closing_date=JAN31,
        lines=AT3_LINES, account=IBANS["RIYAD-SAR"]), user=w.a)
    charge = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge",
                                              statement_line=imp.lines.get(line_no=3))
    services.submit_bank_transaction(charge, user=w.a)
    services.approve_bank_transaction(charge, user=w.p)
    rec = services.prepare_reconciliation(accounts.riyad, FiscalPeriod.objects.get(entity=w.alm, name="2027-01"),
                                          user=w.a)
    expected = {"bank_account_created": None, "import_accepted": 1, "match_confirmed": 2,
                "bank_transaction_created": 1, "bank_transaction_submitted": 1, "bank_transaction_approved": 1,
                "reconciliation_prepared": 1}
    for event, count in expected.items():
        rows = BankingEvent.objects.filter(event=event)
        assert rows.exists(), event
        if count:
            assert rows.count() == count, event
        assert all(r.actor_id for r in rows)
    assert BankingEvent.objects.get(event="bank_transaction_approved").actor == w.p
    full = IBANS["RIYAD-SAR"]
    assert not any(full in str(e.details) for e in BankingEvent.objects.all())
    event = BankingEvent.objects.first()
    event.details = {"x": 1}
    with pytest.raises(BankingError):
        event.save()
    with pytest.raises(BankingError):
        event.delete()
    with pytest.raises(BankingError):
        BankingEvent.objects.filter(pk=event.pk).update(details={})
    with pytest.raises(BankingError):
        BankingEvent.objects.all().delete()
    assert rec.status == "draft"


# --- R47 / AT-32 -------------------------------------------------------------------------------------------------

def test_at32_a_new_entity_banks_with_data_only(w):
    new = make_entity("NEW", "full", w.alm)
    bank = banking_models.Bank.objects.create(code="testbank", name_en="Test Bank", name_ar="بنك تجريبي", country="SA",
                                              sa_bank_code="99", verified=True)
    gl = install_bank_sub_account(new, "1101", "Test Bank SAR", "بنك تجريبي ريال", "SAR")
    acc, app = make_user("new_acc"), make_user("new_app")
    member(acc, new, "accountant")
    member(app, new, "approver")
    w.users["NEW"] = {"acc": acc, "app": app}
    account = open_account(w, new, "TEST-SAR", bank, gl, "SAR", "islamic", opening="0.00",
                           iban=make_iban("99", "000000000000500001"))
    entry = journal(w, new, date(2027, 1, 10), [{"account": "5300", "debit": D("640.00")},
                                                {"account": "1101", "credit": D("640.00")}], reference="NEWREF-1")
    imp = statement(w, account, [(date(2027, 1, 11), "D", "640.00", "NTRF", "", "NEWREF-1", ""),
                                 (date(2027, 1, 12), "D", "15.00", "NCHG", "", "", "CHARGES")], start=JAN1, end=JAN31)
    assert Match.objects.get(bank_account=account).status == "confirmed"  # AT-11 for NEW
    txn = services.create_bank_transaction(account, user=acc, kind="bank_charge", statement_line=imp.lines.get(line_no=2))
    services.submit_bank_transaction(txn, user=acc)
    assert services.approve_bank_transaction(txn, user=app).number == "NEW-BTX-2027-000001"
    assert entry.lines.get(account__code="1101").pk in set(MatchPart.objects.values_list("journal_line_id", flat=True))


def _source_files():
    return [p for p in BANKING.rglob("*.py") if not (p.name.startswith("test_") or p.name in ("conftest.py",
                                                                                         "testing.py"))
            and "migrations" not in p.parts]


def test_r47_no_entity_code_in_banking_code():
    codes = re.compile(r"""["'](ALM|SUB|NSH|MIN|OTHER|NEW)[-"']""")
    hits = [f"{p.name}:{n}" for p in _source_files() for n, line in enumerate(p.read_text().splitlines(), 1)
            if codes.search(line)]
    assert not hits, hits


# --- R46: bilingual -----------------------------------------------------------------------------------------------

def _literal(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return None


def test_r46_every_refusal_and_label_has_arabic():
    missing = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            f = call.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            texts = []
            if name in ("error", "parse_error", "bi") and len(call.args) >= 2:
                texts = [_literal(call.args[1])]
            elif name == "append" and isinstance(f.value, ast.Name) and f.value.id in ("problems", "out", "warnings"):
                texts = [_literal(call.args[0])]
            elif name == "_Refused" and call.args and isinstance(call.args[0], ast.List):
                texts = [_literal(e) for e in call.args[0].elts]
            for text in texts:
                if text is not None and re.search(r"[A-Za-z]", text) and not ARABIC.search(text):
                    missing.append(f"{path.name}:{call.lineno}: {text[:60]!r}")
    assert not missing, "\n".join(missing)
    for model in (banking_models.BankAccount, banking_models.BankTransaction, banking_models.Match,
                  banking_models.Reconciliation, banking_models.StatementLine, banking_models.BankingEvent):
        assert ARABIC.search(str(model._meta.verbose_name)), model
        for field in model._meta.get_fields():
            for _, label in getattr(field, "choices", None) or ():
                assert ARABIC.search(str(label)), (model, field.name, label)


@pytest.mark.django_db
def test_r46_seeded_banks_are_bilingual():
    banks = banking_models.Bank.objects.all()
    assert {b.code for b in banks} >= {"riyad", "snb", "alrajhi", "aljazira", "alinma"}
    assert all(b.name_en and ARABIC.search(b.name_ar) for b in banks)


# --- database guards (R15, R35, R40, R45) --------------------------------------------------------------------------

def test_status_and_documents_change_only_through_services(w, accounts):
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("5.00"),
                                           date=date(2027, 1, 5))
    txn.status = "posted"
    with pytest.raises(BankingError):
        txn.save()
    with pytest.raises(BankingError):
        BankTransaction.objects.filter(pk=txn.pk).update(status="posted")
    with pytest.raises(BankingError):
        BankTransaction.objects.get(pk=txn.pk).delete()
    services.submit_bank_transaction(txn, user=w.a)
    posted = services.approve_bank_transaction(txn, user=w.p)
    posted.amount = D("6.00")
    posted._service_write = True
    with pytest.raises(BankingError, match="never edited"):
        posted.save()
    account = BankAccount.objects.get(pk=accounts.riyad.pk)
    account.entity = w.sub
    account._service_write = True
    with pytest.raises(BankingError):
        account.save()


@postgres_only
def test_triggers_refuse_raw_sql_changes(w, accounts):
    from banking.conftest import receipt

    receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    statement(w, accounts.riyad, [(date(2027, 1, 15), "C", "11475.00", "NTRF", "", "TRX88", "")], start=JAN1, end=JAN31)
    match = Match.objects.get(bank_account=accounts.riyad)
    txn = services.create_bank_transaction(accounts.riyad, user=w.a, kind="bank_charge", amount=D("5.00"),
                                           date=date(2027, 1, 5))
    services.submit_bank_transaction(txn, user=w.a)
    services.approve_bank_transaction(txn, user=w.p)
    event = BankingEvent.objects.first()
    statements = [
        ("UPDATE banking_bankingevent SET event = 'x' WHERE id = %s", event.pk),
        ("DELETE FROM banking_bankingevent WHERE id = %s", event.pk),
        ("UPDATE banking_matchpart SET amount = 1 WHERE match_id = %s", match.pk),
        ("DELETE FROM banking_match WHERE id = %s", match.pk),
        ("UPDATE banking_match SET status = 'proposed' WHERE id = %s", match.pk),
        ("UPDATE banking_banktransaction SET amount = 6 WHERE id = %s", txn.pk),
        ("DELETE FROM banking_banktransaction WHERE id = %s", txn.pk),
        ("UPDATE banking_bankaccount SET entity_id = %s WHERE id = " + str(accounts.riyad.pk), w.sub.pk),
        ("DELETE FROM banking_bankaccount WHERE id = %s", accounts.rajhi.pk),
    ]
    for sql, param in statements:
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [param])


@postgres_only
def test_triggers_freeze_an_approved_reconciliation_and_locked_matches(w, accounts):
    from banking.conftest import receipt

    receipt(w, "11475.00", date(2027, 1, 14), bank_reference="TRX88")
    statement(w, accounts.riyad, [(date(2027, 1, 15), "C", "11475.00", "NTRF", "", "TRX88", "")], start=JAN1, end=JAN31)
    rec = services.prepare_reconciliation(accounts.riyad, FiscalPeriod.objects.get(entity=w.alm, name="2027-01"),
                                          user=w.a)
    services.submit_reconciliation(rec, user=w.a)
    services.approve_reconciliation(rec, user=w.p)
    match = Match.objects.get(bank_account=accounts.riyad)
    for sql, param in (("UPDATE banking_reconciliation SET difference = 1 WHERE id = %s", rec.pk),
                       ("UPDATE banking_reconciliation SET status = 'draft' WHERE id = %s", rec.pk),
                       ("UPDATE banking_match SET status = 'undone' WHERE id = %s", match.pk),
                       ("DELETE FROM banking_reconciliation WHERE id = %s", rec.pk)):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [param])
