"""Invoicing R97 (policy A, D32(a), amended by D33(e)); ledger-fx R12 note D12(b); AT-115 (ledger side).

Manual journals never post to the sub-ledger control accounts 1200, 1210, 2100 and 2110 (``control_account``, en/ar).
Registered document types post to them only when they declared the account in ``ledger.documents``
(``control_accounts``, the registry's allow-list); the migration opening entry is the one manual exception (its
cases with real ``ImportedEntry`` rows are in ``migration/test_control_accounts_r97.py``); revaluation lines
(ledger-fx R21, policy C) and reversals still post. The invoicing and banking cases of AT-115 are in
``invoicing/test_control_accounts_r97.py`` and ``banking/test_control_accounts_r97.py``.
"""
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core import access
from core.access.models import GrantUse
from core.access.testing import make_user, member
from core.models import Entity
from ledger import documents, services
from ledger.models import ExchangeRate, JournalEntry, JournalLine, LedgerError
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts
from testdocs.models import Bill, SalesDocument, Unregistered, validate_lines

pytestmark = pytest.mark.django_db
D = Decimal
ON = date(2027, 1, 10)
CONTROL = ("1200", "1210", "2100", "2110")


def setup_entity(code, consolidation="full", parent=None):
    e = Entity.objects.create(code=code, name_en=code, name_ar=f"كيان {code}", consolidation=consolidation, parent=parent)
    install_chart_of_accounts(e)
    create_monthly_periods(e, 2027)
    return e


@pytest.fixture
def w():
    alm = setup_entity("ALM", "parent")
    sub = setup_entity("SUB", "full", alm)
    clerk, cfo = make_user("clerk"), make_user("cfo")
    member(clerk, alm, "accountant")
    member(cfo, alm, "approver")
    return SimpleNamespace(alm=alm, sub=sub, clerk=clerk, cfo=cfo)


def on_control(w, code, amount="100.00"):
    """Dr/Cr ``code`` against income or expense, with the counterparty an intercompany account needs."""
    cp = w.sub if code in ("1210", "2110") else None
    if code.startswith("1"):
        return [{"account": code, "debit": D(amount), "counterparty": cp}, {"account": "4100", "credit": D(amount)}]
    return [{"account": "5900", "debit": D(amount)}, {"account": code, "credit": D(amount), "counterparty": cp}]


def post_grant(w, entry, user=None):
    return access.require(user or w.cfo, entry.entity, "ledger.post_entry", document=entry)


def post_bill(w, lines, bill=None, on=ON):
    bill = bill or Bill.objects.create(entity=w.alm, created_by=w.clerk,
                                       amount=sum((D(l.get("debit") or 0) for l in lines), D("0")))
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill)
    return services.create_entry(w.alm, on, "Bill", lines, user=w.clerk, post=True, grant=grant, document=bill)


def assert_control_refusal(exc, code):
    assert exc.code == "control_account"
    text = " ".join(exc.messages)
    assert f"Account {code}" in text and "control account" in text and "manual journal" in text  # English
    assert f"الحساب {code}" in text and "حساب مراقبة" in text and "بقيد يدوي" in text  # Arabic


# --- manual journals (AT-115 first case) ------------------------------------------------------------------------

def test_at115_manual_journal_dr_1200_cr_4100_is_refused_and_nothing_is_written(w):
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, ON, "Manual / يدوي", on_control(w, "1200"), user=w.clerk)
    assert_control_refusal(exc.value, "1200")
    assert not JournalEntry.objects.filter(entity=w.alm).exists()


@pytest.mark.parametrize("code", CONTROL)
def test_r97_every_subledger_control_account_is_refused_in_a_manual_journal(w, code):
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.alm, ON, "Manual", on_control(w, code), user=w.clerk)
    assert_control_refusal(exc.value, code)


def test_r97_the_refusal_applies_in_every_entity(w):
    member(w.clerk, w.sub, "accountant")
    lines = [{"account": "1210", "debit": D("5.00"), "counterparty": w.alm}, {"account": "4100", "credit": D("5.00")}]
    with pytest.raises(LedgerError) as exc:
        services.create_entry(w.sub, ON, "Manual", lines, user=w.clerk)
    assert_control_refusal(exc.value, "1210")


def test_r97_a_draft_cannot_be_changed_to_carry_a_control_line(w):
    entry = services.create_entry(w.alm, ON, "Manual", [{"account": "1100", "debit": D("5.00")},
                                                       {"account": "4100", "credit": D("5.00")}], user=w.clerk)
    with pytest.raises(LedgerError) as exc:
        services.update_draft(entry, user=w.clerk, lines=on_control(w, "2100"))
    assert_control_refusal(exc.value, "2100")
    assert set(entry.lines.values_list("account__code", flat=True)) == {"1100", "4100"}


def test_at115_import_journal_without_imported_entry_is_refused_at_posting(w):
    """A person may draft an import (``source="import"``; the migration opening entry is linked to its
    ``ImportedEntry`` after the draft exists), but without that link it never posts on a control account."""
    entry = services.create_entry(w.alm, ON, "Import", on_control(w, "1200"), user=w.clerk, source="import")
    grant = post_grant(w, entry)
    with pytest.raises(LedgerError) as exc:
        services.post_entry(entry, grant)
    assert_control_refusal(exc.value, "1200")
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"
    assert not GrantUse.objects.filter(log_id=grant.log_id).exists()  # the refused posting used nothing


def test_r97_validate_entry_lists_the_control_refusal(w):
    entry = services.create_entry(w.alm, ON, "Import", on_control(w, "2110"), user=w.clerk, source="import")
    problems = services.validate_entry(entry)
    assert any("Account 2110" in p and "حساب مراقبة" in p for p in problems)


def test_r97_tax_control_accounts_stay_open_to_manual_journals(w):
    """Spec conflict recorded in invoicing R97 (as built): tax-returns R16(a) flags manual journals on 2200 / 1310
    (``untagged_vat_journal``) and R26 / R27 correct a filed return with correcting journal entries on 2200 / 1310,
    so the tax control accounts (1310, 2200, 2220) are not refused here; only the sub-ledger control accounts are."""
    entry = services.create_entry(w.alm, ON, "VAT correction", [{"account": "1100", "debit": D("15.00")},
                                                               {"account": "2200", "credit": D("15.00")}], user=w.clerk)
    assert services.post_entry(entry, post_grant(w, entry)).status == "posted"


# --- admin (same refusal, from ledger.services) ----------------------------------------------------------------

def _staff(user):
    user.is_staff = True
    user.save()
    return user


def test_r97_admin_manual_journal_on_a_control_account_is_refused(client, w):
    from ledger.models import Account

    client.force_login(_staff(w.clerk))
    data = {
        "entity": w.alm.pk, "date": "2027-01-07", "description": "Admin AR journal", "reference": "",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-account": Account.objects.get(entity=w.alm, code="1200").pk, "lines-0-debit": "5", "lines-0-credit": "0",
        "lines-1-account": Account.objects.get(entity=w.alm, code="4100").pk, "lines-1-debit": "0", "lines-1-credit": "5",
    }
    resp = client.post("/admin/ledger/journalentry/add/", data, follow=True)
    page = resp.content.decode()
    assert "Not saved" in page and "control account" in page and "حساب مراقبة" in page
    assert not JournalEntry.objects.filter(description="Admin AR journal").exists()


def test_r97_admin_post_action_refuses_a_control_account_draft(client, w):
    """An import draft on 1200 with no ImportedEntry (the only kind of manual draft that can carry the line)."""
    from datetime import timedelta

    import pyotp
    from django.utils import timezone

    from web import mfa
    from web.models import MfaDevice

    entry = services.create_entry(w.alm, ON, "Import", on_control(w, "1200"), user=w.clerk, source="import")
    cfo = _staff(w.cfo)
    client.force_login(cfo)
    if mfa.device_of(cfo) is None:
        device = mfa.start_enrolment(cfo)
        mfa.confirm_enrolment(cfo, pyotp.TOTP(device.secret).now())
    code = pyotp.TOTP(MfaDevice.objects.get(user=cfo).secret).at(timezone.now() + timedelta(seconds=30))
    resp = client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [entry.pk],
                                                       "mfa_confirm": "1", "mfa_code": code}, follow=True)
    assert "control account" in resp.content.decode()
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"


# --- registered documents (allow-list) ------------------------------------------------------------------------

def test_at115_a_registered_type_that_declared_2100_posts_on_it(w):
    entry = post_bill(w, on_control(w, "2100"))
    assert entry.status == "posted" and entry.lines.filter(account__code="2100").exists()
    assert documents.get("testdocs.bill").control_codes() == {"1200", "1210", "2100", "2110"}


def test_r97_reversal_of_an_allowed_entry_still_posts(w):
    entry = post_bill(w, on_control(w, "2110"))
    bill = Bill.objects.get(pk=entry.document_id)
    rev = services.reverse_entry(entry, access.require(w.clerk, w.alm, "invoicing.reverse_document", document=bill))
    assert rev.status == "posted" and JournalEntry.objects.get(pk=entry.pk).status == "reversed"
    assert rev.lines.get(account__code="2110").debit == D("100.00")


def test_r97_a_registered_type_that_did_not_declare_the_account_is_refused(w):
    with documents._registered_for_tests(Bill, post_permissions={"invoicing.approve_bill"},
                                         reverse_permissions={"invoicing.reverse_document"},
                                         validate_lines=validate_lines,
                                         control_accounts={"2100"}):  # declares 2100 only
        assert post_bill(w, on_control(w, "2100")).status == "posted"
        bill = Bill.objects.create(entity=w.alm, created_by=w.clerk, amount=D("100.00"))
        grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill)
        with pytest.raises(LedgerError) as exc:
            services.create_entry(w.alm, ON, "Bill", on_control(w, "1200"), user=w.clerk, post=True, grant=grant,
                                  document=bill)
        assert_control_refusal(exc.value, "1200")
        assert not JournalEntry.objects.filter(document_type="testdocs.bill", document_id=bill.pk).exists()
        assert not GrantUse.objects.filter(log_id=grant.log_id).exists()


def test_r97_a_predicate_decides_per_document_and_only_true_allows(w):
    seen = []

    def only_big(document):
        seen.append(document.pk)
        return 1 if document.amount < D("100.00") else document.amount >= D("100.00")  # 1 is truthy, not True

    with documents._registered_for_tests(Bill, post_permissions={"invoicing.approve_bill"},
                                         validate_lines=validate_lines, control_accounts={"1200": only_big}):
        assert post_bill(w, on_control(w, "1200", "100.00")).status == "posted"
        with pytest.raises(LedgerError) as exc:
            post_bill(w, on_control(w, "1200", "50.00"))
        assert_control_refusal(exc.value, "1200")
    assert len(seen) >= 2  # the predicate received the stored documents


def test_r97_the_allow_list_cannot_be_widened_by_passing_an_unregistered_document(w):
    doc = Unregistered.objects.create(entity=w.alm, created_by=w.clerk, amount=D("100.00"))
    grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=doc)
    with pytest.raises(access.AccessDenied) as exc:
        services.create_entry(w.alm, ON, "Unregistered", on_control(w, "1200"), user=w.clerk, post=True, grant=grant,
                              document=doc)
    assert exc.value.code == "document_not_registered"
    assert not JournalEntry.objects.filter(entity=w.alm).exists()


def test_r97_an_entry_claiming_an_unregistered_document_type_is_refused(w):
    """validate_entry looks the type up in the registry: a label nobody registered allows nothing."""
    entry = services.create_entry(w.alm, ON, "Import", on_control(w, "1200"), user=w.clerk, source="import")
    entry.document_type, entry.document_id = "testdocs.unregistered", 1  # in memory only
    assert any("Account 1200" in p for p in services.validate_entry(entry))
    entry.document_type = "testdocs.bill"  # a registered type, but no such stored document
    entry.document_id = 10 ** 9
    assert any("Account 1200" in p for p in services.validate_entry(entry))


def test_r97_registration_accepts_only_control_codes_and_is_frozen(w):
    for bad in ({"1100"}, {"2200"}, "1200", {"1200": "not callable"}):
        with pytest.raises(documents.DocumentRegistryError):
            documents._register(Unregistered, post_permissions={"invoicing.approve_bill"},
                                validate_lines=validate_lines, control_accounts=bad)
    assert documents.get("testdocs.unregistered") is None
    with pytest.raises(documents.DocumentRegistryError, match="frozen"):  # no app widens it after start-up
        documents.register(SalesDocument, post_permissions={"invoicing.approve_sales_document"},
                           validate_lines=validate_lines, control_accounts={"1200"})


def test_r97_declarations_in_the_registry(w):
    """The allow-list as data: invoicing's types and banking's intercompany transfer (1210 / 2110 only)."""
    reg = documents.registered()
    assert reg["invoicing.salesdocument"].control_codes() == {"1200", "1210"}
    assert reg["invoicing.customerreceipt"].control_codes() == {"1200", "1210"}
    assert reg["invoicing.purchasedocument"].control_codes() == {"2100", "2110"}
    assert reg["invoicing.vendorpayment"].control_codes() == {"2100", "2110"}
    assert reg["invoicing.migratedsettlement"].control_codes() == {"1200", "1210", "2100", "2110"}
    assert reg["banking.banktransaction"].control_codes() == {"1210", "2110"}
    others = {label for label, t in reg.items() if t.control_accounts and not label.startswith(("invoicing.", "testdocs."))}
    assert others == {"banking.banktransaction"}  # the only non-invoicing type (OQ-66 default)
    assert not reg["invoicing.purificationtransfer"].control_accounts
    assert not reg["invoicing.deferredprofitrelease"].control_accounts


# --- FX revaluation (ledger-fx R21, policy C) -----------------------------------------------------------------

def test_r97_fx_revaluation_of_a_control_account_still_posts_and_reverses(w):
    ExchangeRate.objects.create(from_currency_id="EUR", to_currency_id="SAR", date=date(2027, 1, 15),
                                rate=D("4.1000"), source="manual", created_by=make_user())
    ExchangeRate.objects.create(from_currency_id="EUR", to_currency_id="SAR", date=date(2027, 1, 31),
                                rate=D("4.0800"), source="manual", created_by=make_user())
    invoice = Bill.objects.create(entity=w.alm, created_by=w.clerk, amount=D("41000.00"))  # a registered stand-in
    post_bill(w, [{"account": "1200", "currency": "EUR", "txn_debit": D("10000.00")},
                  {"account": "4100", "currency": "EUR", "txn_credit": D("10000.00")}], bill=invoice,
              on=date(2027, 1, 15))
    run = services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=w.clerk)
    run = services.post_revaluation(run, access.require(w.cfo, w.alm, "ledger.post_entry", document=run.entry))
    entry = JournalEntry.objects.get(pk=run.entry_id)
    line = entry.lines.get(account__code="1200")
    assert (line.credit, line.line_kind) == (D("200.00"), "revaluation") and entry.document_type == ""
    assert JournalEntry.objects.get(pk=run.reversal_id).status == "posted"


def test_r97_a_revaluation_entry_allows_only_its_revaluation_lines(w):
    """A normal line on a control account inside a revaluation entry is not covered by the exception."""
    from ledger.models import RevaluationRun

    entry = services.create_entry(w.alm, date(2027, 1, 31), "Import", on_control(w, "1200"), user=w.clerk,
                                  source="import")
    RevaluationRun.objects.create(entity=w.alm, period=w.alm.periods.get(name="2027-01"), rate_date=date(2027, 1, 31),
                                  run_by=w.clerk, entry=entry)
    assert JournalLine.objects.filter(entry=entry, account__code="1200", line_kind="normal").exists()
    assert any("Account 1200" in p for p in services.validate_entry(entry))
