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


# --- review of 3b4cb6e: reversal chains, predicates, import drafts, revaluation corrections ------------------------

def _legacy_manual_journal(w, code="1200", on=ON):
    """A manual journal on a control account posted before R97 existed (simulated by switching the check off)."""
    from unittest import mock

    entry = services.create_entry(w.alm, on, "Before R97", on_control(w, code), user=w.clerk, source="import")
    with mock.patch.object(services, "_control_problems", return_value=[]):
        return services.post_entry(entry, post_grant(w, entry))


def _reverse(w, entry, on=None):
    return services.reverse_entry(entry, access.require(w.clerk, w.alm, "ledger.reverse_entry", document=entry),
                                  date=on)


def test_review_reversal_of_a_manual_origin_entry_posts_also_at_a_new_date(w):
    legacy = _legacy_manual_journal(w)
    rev = _reverse(w, legacy, on=date(2027, 2, 3))
    assert (rev.status, rev.date) == ("posted", date(2027, 2, 3))
    assert rev.lines.get(account__code="1200").credit == D("100.00")


def test_review_reversing_a_reversal_of_a_manual_origin_entry_is_refused(w):
    """It would book the manual journal's 1200 line again, at any date."""
    legacy = _legacy_manual_journal(w)
    rev = _reverse(w, legacy)
    with pytest.raises(LedgerError) as exc:
        _reverse(w, rev, on=date(2027, 3, 1))
    assert_control_refusal(exc.value, "1200")
    text = " ".join(exc.value.messages)
    assert "Reversing a reversal books the original lines again" in text and "عكس قيد العكس" in text
    assert JournalEntry.objects.get(pk=rev.pk).status == "posted"
    assert not JournalEntry.objects.filter(reversal_of=rev, status="posted").exists()


def test_review_a_document_posting_may_be_booked_again_by_reversing_its_reversal(w):
    entry = post_bill(w, on_control(w, "2100"))
    bill = Bill.objects.get(pk=entry.document_id)

    def reverse_doc(e):
        return services.reverse_entry(e, access.require(w.clerk, w.alm, "invoicing.reverse_document", document=bill))

    rev = reverse_doc(entry)
    again = reverse_doc(rev)  # the declared type may book 2100; testdocs.bill has no reverse_guard
    assert again.status == "posted" and again.lines.get(account__code="2100").credit == D("100.00")


def test_review_the_documents_reverse_guard_still_decides_a_reversal_of_a_reversal(w):
    allow = {"reverse": True}
    with documents._registered_for_tests(Bill, post_permissions={"invoicing.approve_bill"},
                                         reverse_permissions={"invoicing.reverse_document"},
                                         validate_lines=validate_lines, control_accounts={"2100"},
                                         reverse_guard=lambda d: allow["reverse"]):
        entry = post_bill(w, on_control(w, "2100"))
        bill = Bill.objects.get(pk=entry.document_id)
        rev = services.reverse_entry(entry, access.require(w.clerk, w.alm, "invoicing.reverse_document", document=bill))
        allow["reverse"] = False
        with pytest.raises(access.AccessDenied) as exc:
            services.reverse_entry(rev, access.require(w.clerk, w.alm, "invoicing.reverse_document", document=bill))
        assert exc.value.code == "reverse_through_owner"


@pytest.mark.parametrize("code", ["1210", "2110"])
def test_review_a_registered_type_whose_predicate_says_no_is_refused_at_posting(w, code):
    """End to end, as a non-intercompany bank transaction would be: the type declares 1210 / 2110 behind a predicate
    and the predicate refuses this document."""
    with documents._registered_for_tests(Bill, post_permissions={"invoicing.approve_bill"},
                                         validate_lines=validate_lines,
                                         control_accounts={"1210": lambda d: False, "2110": lambda d: False}):
        bill = Bill.objects.create(entity=w.alm, created_by=w.clerk, amount=D("100.00"))
        grant = access.require(w.cfo, w.alm, "invoicing.approve_bill", document=bill)
        with pytest.raises(LedgerError) as exc:
            services.create_entry(w.alm, ON, "Bill", on_control(w, code), user=w.clerk, post=True, grant=grant,
                                  document=bill)
    assert_control_refusal(exc.value, code)
    assert "only the documents allowed on it post there" in " ".join(exc.value.messages)
    assert not JournalEntry.objects.filter(entity=w.alm).exists()


def test_review_an_import_draft_without_imported_entry_never_posts(w):
    entry = services.create_entry(w.alm, ON, "Import", on_control(w, "1200"), user=w.clerk, source="import")
    for _ in range(2):  # changed or not, it stays a draft (to be discarded)
        with pytest.raises(LedgerError) as exc:
            services.post_entry(entry, post_grant(w, entry))
        assert exc.value.code == "control_account"
        services.update_draft(entry, user=w.clerk, lines=on_control(w, "1200", "90.00"))
    assert JournalEntry.objects.get(pk=entry.pk).status == "draft"


# --- OQ-9 team default: a wrong revaluation of 1200 is corrected by reversing the run and running it again --------

def _rate(on, value, source="manual"):
    return ExchangeRate.objects.create(from_currency_id="EUR", to_currency_id="SAR", date=on, rate=D(value),
                                       source=source, created_by=make_user())


def _eur_invoice(w):
    _rate(date(2027, 1, 15), "4.1000")
    invoice = Bill.objects.create(entity=w.alm, created_by=w.clerk, amount=D("41000.00"))
    return post_bill(w, [{"account": "1200", "currency": "EUR", "txn_debit": D("10000.00")},
                         {"account": "4100", "currency": "EUR", "txn_credit": D("10000.00")}], bill=invoice,
                     on=date(2027, 1, 15))


def _revalue(w, period="2027-01"):
    run = services.revalue_period(w.alm, w.alm.periods.get(name=period), user=w.clerk)
    return services.post_revaluation(run, access.require(w.cfo, w.alm, "ledger.post_entry", document=run.entry))


def _balance(w, code, on):
    return sum((l.debit - l.credit for l in JournalLine.objects.filter(
        entry__entity=w.alm, account__code=code, entry__date__lte=on, entry__status__in=["posted", "reversed"])), D("0"))


def test_oq9_wrong_revaluation_of_1200_is_reversed_and_run_again_at_the_right_rate(w):
    _eur_invoice(w)
    _rate(date(2027, 1, 31), "4.0000", source="file")  # the wrong closing rate
    wrong = _revalue(w)
    assert _balance(w, "1200", date(2027, 1, 31)) == D("40000.00")
    _rate(date(2027, 1, 31), "4.0800")  # the right one, entered as a new row (R11); manual wins on the date
    with pytest.raises(LedgerError) as exc:  # the period is revalued: a second run waits for the correction
        services.revalue_period(w.alm, w.alm.periods.get(name="2027-01"), user=w.clerk)
    assert exc.value.code == "already_revalued"
    with pytest.raises(LedgerError) as exc:  # not by reversing its entries one by one
        _reverse(w, JournalEntry.objects.get(pk=wrong.reversal_id))
    assert exc.value.code == "use_reverse_revaluation"

    grant = access.require(w.clerk, w.alm, "ledger.reverse_entry", document=wrong.entry)
    reversed_run = services.reverse_revaluation(wrong, grant, reason="Wrong closing rate / سعر إقفال خاطئ")
    assert (reversed_run.status, reversed_run.reversed_by) == ("reversed", w.clerk)
    k1, k2 = (JournalEntry.objects.get(pk=pk) for pk in (reversed_run.correction_entry_id,
                                                         reversed_run.correction_reversal_id))
    assert (k1.date, k1.status, k2.date, k2.status) == (date(2027, 1, 31), "posted", date(2027, 2, 1), "posted")
    assert k1.lines.get(account__code="1200").line_kind == "revaluation"
    assert _balance(w, "1200", date(2027, 1, 31)) == D("41000.00") == _balance(w, "1200", date(2027, 2, 28))

    right = _revalue(w)
    assert right.status == "posted"
    assert _balance(w, "1200", date(2027, 1, 31)) == D("40800.00")
    assert _balance(w, "5760", date(2027, 1, 31)) == D("200.00")
    assert _balance(w, "1200", date(2027, 2, 28)) == D("41000.00")  # every revaluation is undone the next day
    with pytest.raises(LedgerError) as exc:  # a reversed run is reversed once
        services.reverse_revaluation(reversed_run, access.require(w.clerk, w.alm, "ledger.reverse_entry",
                                                                  document=wrong.entry), reason="again")
    assert exc.value.code == "run_not_posted"


def test_oq9_a_revaluation_is_reversed_only_while_its_period_is_open_and_nothing_later_ran(w):
    from ledger.models import FiscalPeriod, RevaluationRun

    _eur_invoice(w)
    _rate(date(2027, 1, 31), "4.0000")
    jan = _revalue(w)
    _rate(date(2027, 2, 28), "4.0500")
    _revalue(w, "2027-02")

    def attempt(reason="Wrong rate"):
        grant = access.require(w.clerk, w.alm, "ledger.reverse_entry", document=jan.entry)
        return services.reverse_revaluation(RevaluationRun.objects.get(pk=jan.pk), grant, reason=reason)

    with pytest.raises(LedgerError) as exc:
        attempt()
    assert exc.value.code == "later_revaluation"
    with pytest.raises(LedgerError) as exc:
        attempt(reason=" ")
    assert exc.value.code == "reason_required"
    services.close_period(FiscalPeriod.objects.get(entity=w.alm, name="2027-01"), user=w.cfo)
    with pytest.raises(LedgerError, match="closed"):
        attempt()
    assert RevaluationRun.objects.get(pk=jan.pk).status == "posted"


# --- re-review of d02fc9f: N2 to N4 ----------------------------------------------------------------------------

def test_n2_reverse_revaluation_is_refused_to_agents_and_system_users(w):
    from unittest import mock

    from core.access.testing import make_agent

    _eur_invoice(w)
    _rate(date(2027, 1, 31), "4.0000")
    run = _revalue(w)
    grant = access.require(w.clerk, w.alm, "ledger.reverse_entry", document=run.entry)
    for user, code in ((make_agent("fx_agent"), "agent_forbidden"), (access.system_user("fx_import"),
                                                                       "system_forbidden")):
        with mock.patch.object(services.access, "verify_grant", return_value=user):  # a grant of theirs, if any
            with pytest.raises(access.AccessDenied) as exc:
                services.reverse_revaluation(run, grant, reason="Wrong rate")
        assert exc.value.code == code
    from ledger.models import RevaluationRun

    assert RevaluationRun.objects.get(pk=run.pk).status == "posted"
    assert services.reverse_revaluation(run, grant, reason="Wrong rate").status == "reversed"  # a person may


@pytest.mark.skipif(not __import__("django.db").db.connection.vendor == "postgresql",
                    reason="PostgreSQL-only database guard")
def test_n3_pg_trigger_requires_a_reason_and_four_distinct_entries_for_a_reversed_run(w):
    from django.db import DatabaseError, connection, transaction

    _eur_invoice(w)
    _rate(date(2027, 1, 31), "4.0000")
    run = _revalue(w)
    k1, k2 = (services.create_entry(w.alm, date(2027, 1, 31), f"x{i}", [{"account": "1100", "debit": 1},
                                                                       {"account": "3100", "credit": 1}],
                                    user=w.clerk) for i in (1, 2))
    sql = ("UPDATE ledger_revaluationrun SET status = 'reversed', reversed_by_id = %s, reversed_at = now(), "
           "correction_entry_id = %s, correction_reversal_id = %s, reversal_reason = %s WHERE id = %s")
    for k1_id, k2_id, reason in ((k1.pk, k2.pk, ""), (k1.pk, k2.pk, "   "), (k1.pk, k1.pk, "r"),
                                 (run.entry_id, k2.pk, "r"), (k1.pk, run.reversal_id, "r")):
        with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(sql, [w.clerk.pk, k1_id, k2_id, reason, run.pk])
    with transaction.atomic(), connection.cursor() as cursor:  # the same write with a reason and distinct entries
        cursor.execute(sql, [w.clerk.pk, k1.pk, k2.pk, "Wrong rate", run.pk])


def test_n4_a_broken_reversal_chain_is_refused_with_control_account(w):
    entry = services.create_entry(w.alm, ON, "Import", on_control(w, "1200"), user=w.clerk, source="import")
    entry.reversal_of_id = 10 ** 9  # in memory only: a link to an entry that does not exist
    with pytest.raises(LedgerError) as exc:
        services.validate_entry(entry)
    assert exc.value.code == "control_account"
    text = " ".join(exc.value.messages)
    assert "does not exist" in text and "غير موجود" in text
