"""migration.md R14 to R19 (loading, posting, discarding, post-load tie-out; AT-7, AT-9 to AT-11, AT-13, AT-14)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from core.access import AccessDenied
from core.access.models import Membership, Role, RolePermission
from core.access.services import now
from core.access.testing import make_agent
from ledger import services as ledger
from ledger.models import JournalEntry
from migration import services
from migration import testing as t
from migration.conftest import BALANCES, CUTOVER, YEAR_END, journal_file, journal_rows, map_rows, tb_file
from migration.models import ImportedEntry, MigrationError

D = Decimal


def ledger_tb(w, as_of):
    return {r["code"]: r["debit"] - r["credit"] for r in ledger.trial_balance(w.alm, as_of, user=w.p)}


def test_at9_opening_from_closing_tb_dry_run_draft_second_person_post_and_tie_out(w, mapped):
    tb = tb_file()
    plan = services.load_opening_balances(w.alm, tb, user=w.a, cutover=YEAR_END)
    assert plan.report.ok and not plan.applied and plan.entries[0]["lines"] == 9
    assert not JournalEntry.objects.filter(entity=w.alm).exists()  # dry run writes nothing

    plan = services.load_opening_balances(w.alm, tb, user=w.a, cutover=YEAR_END, apply=True)
    imp = plan.created[0]
    entry = imp.journal_entry
    assert (entry.status, entry.source, entry.reference, entry.date) == ("draft", "import", "FALCON-OPENING", YEAR_END)
    assert entry.lines.count() == 9 and entry.total_debit == entry.total_credit == D("91500.00")
    assert entry.lines.get(account__code="2200").credit == D("6500.00")
    assert "Falcon 1102 بنك تجريبي" in list(entry.lines.values_list("description", flat=True))
    assert not entry.lines.filter(account__code="3900").exists()  # the full TB balances: nothing to 3900

    rows, problems = services.tie_out_ledger(w.alm, tb, user=w.p, as_of=YEAR_END)
    assert not problems and {r["erp_code"]: r["actual"] for r in rows}["1100"] == D("60500.00")
    rows, problems = services.tie_out_ledger(w.alm, tb, user=w.p, as_of=YEAR_END, include_drafts=False)
    assert len(problems) == 8  # nothing posted yet

    with pytest.raises(AccessDenied):  # the accountant does not post
        services.post_imported_entries(w.alm, user=w.a, kind="opening", apply=True)
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening")
    assert result["ready"] == ["opening"] and not result["posted"]  # dry run
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    posted = result["posted"][0]
    assert (posted.status, posted.posted_by, posted.number) == ("posted", w.p, "ALM-JE-2026-000001")
    rows, problems = services.tie_out_ledger(w.alm, tb, user=w.p, as_of=YEAR_END, include_drafts=False)
    assert not problems
    assert ledger_tb(w, YEAR_END)["3100"] == D("-60000.00")

    again = services.load_opening_balances(w.alm, tb, user=w.a, cutover=YEAR_END, apply=True)
    assert again.report.ok and again.skipped == ["opening"] and not again.created  # idempotent
    balances = dict(BALANCES)
    balances["1101"], balances["1102"] = ("الصندوق الرئيسي", "9000.00", "0", "3000.00"), ("بنك تجريبي", "51000.00",
                                                                                         "11500.00", "8000.00")
    other = services.load_opening_balances(w.alm, tb_file(balances), user=w.a, cutover=YEAR_END, apply=True)
    assert "already_imported" in {p.code for p in other.report.errors} and not other.created


def test_preparer_with_posting_right_still_cannot_post_own_import(w, mapped):
    from core.access.testing import make_user, member
    both = make_user("mig.both")
    member(both, w.alm, "accountant")
    member(both, w.alm, "approver")
    services.load_opening_balances(w.alm, tb_file(), user=both, cutover=YEAR_END, apply=True)
    result = services.post_imported_entries(w.alm, user=both, kind="opening", apply=True)
    assert result["problems"] == ["opening"] and not result["posted"]
    assert "second person" in result["details"]["opening"][0]


def test_at10_draft_changed_after_import_is_not_posted(w, mapped):
    imp = services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=YEAR_END, apply=True).created[0]
    lines = [{"account": l.account, "debit": l.debit, "credit": l.credit, "description": l.description}
             for l in imp.journal_entry.lines.all()]
    lines[0] = {**lines[0], "debit": lines[0]["debit"] + 1 if lines[0]["debit"] else 0,
                "credit": lines[0]["credit"] + 1 if lines[0]["credit"] else 0}
    lines.append({"account": "3900", **({"credit": D("1")} if lines[0]["debit"] else {"debit": D("1")})})
    ledger.update_draft(imp.journal_entry, user=w.a2, lines=lines)
    result = services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    assert result["problems"] == ["opening"] and "changed" in result["details"]["opening"][0]
    assert JournalEntry.objects.get(pk=imp.journal_entry_id).status == "draft"


def test_at11_unbalanced_tb_creates_nothing(w, mapped):
    balances = dict(BALANCES)
    balances["2501"] = ("رأس المال", "-60000.01", "0", "0")
    plan = services.load_opening_balances(w.alm, tb_file(balances), user=w.a, cutover=YEAR_END, apply=True)
    assert "tb_unbalanced_closing" in {p.code for p in plan.report.errors}
    assert not plan.applied and not ImportedEntry.objects.exists() and not JournalEntry.objects.exists()


def test_pl_to_opening_equity_nets_income_and_expense_into_3900(w, mapped):
    tb = tb_file()
    imp = services.load_opening_balances(w.alm, tb, user=w.a, cutover=YEAR_END, pl_to_opening_equity=True,
                                         apply=True).created[0]
    lines = imp.journal_entry.lines
    assert not lines.filter(account__code__in=["4100", "5100", "5200"]).exists()
    assert lines.get(account__code="3900").debit == D("1000.00")  # income 10,000 less expenses 11,000
    _, problems = services.tie_out_ledger(w.alm, tb, user=w.p, as_of=YEAR_END, pl_to_opening_equity=True)
    assert not problems


def test_opening_needs_an_open_cutover_period(w, mapped):
    plan = services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=date(2030, 12, 31), apply=True)
    assert "no_period" in {p.code for p in plan.report.errors} and not plan.applied


def replay(w, journal=None, apply=True, user=None):
    return services.load_journal_entries(w.alm, journal or journal_file(), user=user or w.a, cutover=CUTOVER,
                                         trial_balance=tb_file(), apply=apply)


def test_at7_replay_opening_plus_journals_tagged_idempotent_and_tied_out(w, mapped):
    opening = services.load_opening_balances(w.alm, tb_file(), user=w.a, cutover=CUTOVER, basis="opening", apply=True)
    assert opening.created[0].journal_entry.lines.count() == 6  # P&L codes have no opening balance
    plan = replay(w, apply=False)
    assert plan.report.ok and len(plan.entries) == 4 and not plan.created
    plan = replay(w)
    refs = sorted(JournalEntry.objects.filter(entity=w.alm, reference__startswith="FALCON-2").values_list("reference", flat=True))
    assert refs == ["FALCON-26000001", "FALCON-26000002", "FALCON-26000003", "FALCON-26000004"]
    entry = ImportedEntry.objects.get(legacy_key="26000003").journal_entry
    assert entry.source == "import" and entry.status == "draft" and entry.date == date(2026, 2, 1)
    assert "مركز التكلفة: الإدارة" in entry.lines.get(account__code="5100").description
    first = ImportedEntry.objects.get(legacy_key="26000001").journal_entry
    assert "مستخلص عميل 501" in first.lines.first().description

    again = replay(w)  # idempotent: nothing duplicated
    assert again.report.ok and again.skipped == ["26000001", "26000002", "26000003", "26000004"] and not again.created
    assert ImportedEntry.objects.filter(kind="journal").count() == 4

    rows = journal_rows()
    rows[0]["debit"], rows[1]["credit"] = 11600.0, 10100.0  # same entry number, other amounts
    changed = replay(w, journal_file(rows))
    assert "changed_after_import" in {p.code for p in changed.report.errors} and not changed.created

    _, problems = services.tie_out_ledger(w.alm, tb_file(), user=w.p, as_of=YEAR_END)
    assert not problems  # opening + replayed drafts = TB closing
    services.post_imported_entries(w.alm, user=w.p, kind="opening", apply=True)
    result = services.post_imported_entries(w.alm, user=w.p, kind="journal", apply=True)
    assert len(result["posted"]) == 4 and all(e.status == "posted" for e in result["posted"])
    _, problems = services.tie_out_ledger(w.alm, tb_file(), user=w.p, as_of=YEAR_END, include_drafts=False)
    assert not problems


def test_replay_is_all_or_nothing_when_the_ledger_refuses_one_entry(w, mapped):
    rows = journal_rows() + [t.line(26000005, date(2026, 3, 1), "1101", "الصندوق الرئيسي", debit="1.00"),
                             t.line(26000005, date(2026, 3, 1), "9999", "حساب مجهول", credit="1.00")]
    plan = services.load_journal_entries(w.alm, journal_file(rows), user=w.a, apply=True)
    assert "unmapped" in {p.code for p in plan.report.errors}
    assert not ImportedEntry.objects.exists() and not JournalEntry.objects.exists()


def test_posting_is_all_or_nothing(w, mapped):
    replay(w)
    from ledger.models import FiscalPeriod
    FiscalPeriod.objects.filter(entity=w.alm, name="2026-02").update(status="closed")
    result = services.post_imported_entries(w.alm, user=w.p, kind="journal", apply=True)
    assert sorted(result["problems"]) == ["26000003", "26000004"] and not result["posted"]
    assert not JournalEntry.objects.filter(entity=w.alm, status="posted").exists()


def test_discard_drafts_then_import_again(w, mapped):
    replay(w)
    result = services.discard_imported_drafts(w.alm, user=w.a, kind="journal", legacy_keys=["26000002"])
    assert result == {"drafts": ["26000002"], "posted": []} and ImportedEntry.objects.count() == 4  # dry run
    services.discard_imported_drafts(w.alm, user=w.a, kind="journal", legacy_keys=["26000002"], apply=True)
    assert ImportedEntry.objects.count() == 3 and not JournalEntry.objects.filter(reference="FALCON-26000002").exists()
    plan = replay(w)
    assert [i.legacy_key for i in plan.created] == ["26000002"]
    services.post_imported_entries(w.alm, user=w.p, kind="journal", legacy_keys=["26000001"], apply=True)
    result = services.discard_imported_drafts(w.alm, user=w.a, kind="journal", apply=True)
    assert result["posted"] == ["26000001"] and ImportedEntry.objects.count() == 1


def test_intercompany_code_posts_with_its_counterparty(w):
    rows = map_rows() + [{"legacy_code": "1301", "legacy_name": "مستحق من شركة تابعة", "erp_code": "1210",
                          "counterparty": "SUB"}]
    services.approve_account_map(services.create_account_map(w.alm, user=w.a, rows=rows), user=w.p)
    journal = journal_file([t.line(26000010, date(2026, 3, 5), "1301", "مستحق من شركة تابعة", debit="500.00"),
                            t.line(26000010, date(2026, 3, 5), "1102", "بنك تجريبي", credit="500.00")])
    services.load_journal_entries(w.alm, journal, user=w.a, apply=True)
    entry = services.post_imported_entries(w.alm, user=w.p, kind="journal", apply=True)["posted"][0]
    assert entry.lines.get(account__code="1210").counterparty_entity == w.sub


def test_at13_entity_separation(w, mapped):
    with pytest.raises(AccessDenied):
        services.load_opening_balances(w.alm, tb_file(), user=w.s, cutover=YEAR_END, apply=True)
    replay(w)
    with pytest.raises(AccessDenied):
        services.post_imported_entries(w.alm, user=w.ps, kind="journal", apply=True)
    with pytest.raises(AccessDenied):
        services.discard_imported_drafts(w.alm, user=w.s, kind="journal", apply=True)
    assert list(services.imported_entries(w.s)) == [] and len(services.imported_entries(w.v, w.alm)) == 4


def test_at14_agents_cannot_import(w, mapped):
    role = Role.objects.create(code="agent_migration", name_en="Agent", name_ar="وكيل", scope="entity", kind="agent")
    RolePermission.objects.create(role=role, codename="ledger.create_entry")
    agent = make_agent("migration_helper")
    Membership.objects.create(user=agent, entity=w.alm, role=role, valid_from=now() - timedelta(hours=1),
                              granted_by=w.p, reason="test")
    with pytest.raises((AccessDenied, MigrationError)):
        services.load_opening_balances(w.alm, tb_file(), user=agent, cutover=YEAR_END, apply=True)
    assert not JournalEntry.objects.exists()


def test_rerun_after_posting_and_closing_a_period_skips_imported_entries(w, mapped):
    from ledger.models import FiscalPeriod
    replay(w)
    services.post_imported_entries(w.alm, user=w.p, kind="journal", apply=True)
    FiscalPeriod.objects.filter(entity=w.alm, name="2026-01").update(status="closed")
    rows = journal_rows() + [t.line(26000005, date(2026, 3, 1), "5201", "مصروف إيجار", debit="10.00"),
                             t.line(26000005, date(2026, 3, 1), "1101", "الصندوق الرئيسي", credit="10.00")]
    plan = services.load_journal_entries(w.alm, journal_file(rows), user=w.a, cutover=CUTOVER, apply=True)
    assert plan.report.ok, [p.message for p in plan.report.errors]
    assert [i.legacy_key for i in plan.created] == ["26000005"] and len(plan.skipped) == 4
