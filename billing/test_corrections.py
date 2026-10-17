"""Versions and corrections (billing R38 to R51, D9, D12; AT-22 to AT-26, AT-28 to AT-30, AT-33, AT-34, AT-36,
AT-38, AT-48). Common fixture L1 (§8): ALM commercial lease 10,000.00/month net, S 15%, monthly in advance,
calendar anchor, version 1 approved by C; invoices of the months named are final unless stated."""
from datetime import date
from decimal import Decimal

import pytest

from billing import services
from billing.conftest import approve_note, approved, clear, period, run, version
from billing.models import BillingAdjustment, BillingError, BillingNotification, ContractVersion
from core import access
from invoicing import services as inv
from invoicing.models import DocumentSequence, InvoicingError, SalesDocument
from ledger.models import Account, FiscalPeriod

D = Decimal


def l1(w, months, *, start=date(2027, 1, 1), final=True):
    """L1 approved on its start, invoiced (and cleared) for ``months`` (1 = the start month)."""
    w.clock.set(start)
    c = approved(w, start=start)
    for i in range(months):
        day = date(start.year, start.month + i, 1)
        run(w, day)
        if final:
            clear(w, period(c, day).sales_document)
    return c


def _adjs(c, **filters):
    return list(BillingAdjustment.objects.filter(contract=c, **filters).order_by("billing_period__period_start", "pk"))


def _net(adj):
    return sum((D(x["delta"]) for x in adj.lines), D("0"))


def _position(p):
    """Invoice + debit notes − credit notes of a period's current invoice, counting final notes only (R46d)."""
    doc = SalesDocument.objects.get(pk=p.sales_document_id)
    total = doc.total_net
    for note in SalesDocument.objects.filter(original_document=doc, status="final"):
        total += note.total_net if note.doc_type == "debit_note" else -note.total_net
    return total


# --- AT-22, R38, R39 ---------------------------------------------------------------------------------

def test_at22_version_needs_cfo_and_is_immutable_once_approved(w):
    c = l1(w, 1)
    with pytest.raises(BillingError):  # reason_ar is required at submit
        version(w, c, effective=date(2027, 2, 1), amount="11000.00", reason_ar="")
    services.withdraw_version(c.versions.get(version_no=2), user=w.s)
    v = version(w, c, effective=date(2027, 2, 1), amount="11000.00")
    with pytest.raises(access.AccessDenied):
        services.approve_version(v, user=w.s)
    v = services.approve_version(v, user=w.c)
    c.refresh_from_db()
    assert v.status == "approved" and c.current_version == v
    assert "version_approved" in c.events.values_list("action", flat=True)
    for old in (c.versions.get(version_no=1), v):
        with pytest.raises(BillingError):
            services.update_version(old, user=w.s, reason_ar="تغيير")
        with pytest.raises(BillingError):
            services.update_line(old.lines.get(), user=w.s, amount="1.00")
        stored = ContractVersion.objects.get(pk=old.pk)
        stored.reason_ar = "تغيير"
        stored._service_write = True
        with pytest.raises(BillingError):
            stored.save()
    run(w, date(2027, 2, 1))
    assert period(c, date(2027, 2, 1)).sales_document.total_net == D("11000.00")


def test_r39_creator_or_submitter_never_approves(w):
    c = l1(w, 1)
    v = version(w, c, effective=date(2027, 2, 1), amount="11000.00", user=w.c)  # the CFO drafted and submitted it
    with pytest.raises(access.AccessDenied) as exc:
        services.approve_version(v, user=w.c)
    assert exc.value.code == "self_approval"
    assert services.approve_version(v, user=w.c_b).status == "approved"


def test_at34_retroactive_limits(w):
    c = l1(w, 3)
    with pytest.raises(BillingError):
        version(w, c, effective=date(2026, 12, 1), amount="11000.00")  # before the start
    services.withdraw_version(c.versions.order_by("-version_no").first(), user=w.s)
    for forbidden in ({"currency": "USD"}, {"customer": w.ic_customer}):
        with pytest.raises(BillingError) as exc:
            services.create_version(c, user=w.s, kind="amendment", effective_date=date(2027, 2, 1), reason_ar="تعديل",
                                    **forbidden)
        assert exc.value.code == "not_amendable" and "عقد جديد" in str(exc.value)
    with pytest.raises(BillingError) as exc:
        version(w, c, effective=date(2027, 2, 1), billing_frequency="quarterly", billing_timing="arrears")
    assert "last billed period" in str(exc.value)
    services.withdraw_version(c.versions.order_by("-version_no").first(), user=w.s)
    v = version(w, c, effective=date(2027, 4, 1), billing_frequency="quarterly", billing_timing="arrears")
    assert v.status == "submitted"
    services.approve_version(v, user=w.c)
    assert [(p.period_start, p.period_end) for p in c.periods.filter(status="planned")][:2] == [
        (date(2027, 4, 1), date(2027, 6, 30)), (date(2027, 7, 1), date(2027, 9, 30))]


# --- AT-23, AT-24, R40 to R45, R51 ------------------------------------------------------------------------

def test_at23_retroactive_cancellation_three_draft_credit_notes(w):
    c = l1(w, 3)
    invoices = [period(c, date(2027, m, 1)).sales_document for m in (1, 2, 3)]
    w.clock.set(date(2027, 3, 20))
    v = version(w, c, kind="cancellation", effective=date(2027, 1, 1), reason_ar="العقد لم يبدأ أصلًا")
    v = services.approve_version(v, user=w.c)
    adjs = _adjs(c)
    assert [(a.kind, _net(a), a.status) for a in adjs] == [("credit", D("-10000.00"), "drafted")] * 3
    for adj, original, month in zip(adjs, invoices, (1, 2, 3)):
        note = adj.document
        assert (note.doc_type, note.status, note.original_document_id) == ("credit_note", "draft", original.pk)
        assert (note.supply_date, note.total_net) == (date(2027, month, 1), D("0"))  # totals set at issue
        assert note.lines.get().net_amount == D("10000.00") and note.reason_ar == "العقد لم يبدأ أصلًا"
    c.refresh_from_db()
    assert (c.status, c.termination_kind) == ("terminated", "cancellation")
    assert set(c.periods.filter(period_start__gte=date(2027, 4, 1)).values_list("status", flat=True)) == {"cancelled"}
    assert run(w, date(2027, 4, 1)).generated_count == 0
    summary = BillingNotification.objects.filter(kind="corrections", contract=c, recipient=w.s).get()
    assert "3 correction" in summary.message_en and "التصحيحات" in summary.message_ar
    for adj in adjs:
        final = approve_note(w, adj.document, day=date(2027, 3, 20))
        assert (final.status, final.issue_date, final.total_vat) == ("final", date(2027, 3, 20), D("1500.00"))
        assert sorted((ln.account.code, ln.debit, ln.credit) for ln in final.journal_entry.lines.all()) == [  # B7
            ("1200", D("0.00"), D("11500.00")), ("2200", D("1500.00"), D("0.00")), ("4100", D("10000.00"), D("0.00"))]
        assert final.journal_entry.date == date(2027, 3, 20)
    for original in invoices:
        stored = SalesDocument.objects.get(pk=original.pk)
        assert (stored.status, stored.total_net) == ("final", D("10000.00"))  # never edited
    run(w, date(2027, 4, 2))
    assert [a.status for a in _adjs(c)] == ["final"] * 3


def test_at24_retroactive_termination(w):
    c = l1(w, 3)
    w.clock.set(date(2027, 3, 20))
    v = version(w, c, kind="termination", effective=date(2027, 1, 16), reason_ar="إنهاء مبكر")
    services.approve_version(v, user=w.c)
    jan, feb, mar = _adjs(c)
    assert [_net(a) for a in (jan, feb, mar)] == [D("-5161.29"), D("-10000.00"), D("-10000.00")]
    assert (jan.supply_start, jan.supply_end) == (date(2027, 1, 16), date(2027, 1, 31))
    assert jan.lines[0]["required"] == "4838.71"
    final = approve_note(w, jan.document, day=date(2027, 3, 21))
    assert final.total_vat == D("774.19")
    assert set(c.periods.filter(period_start__gte=date(2027, 4, 1)).values_list("status", flat=True)) == {"cancelled"}


# --- AT-25, AT-26, R42, R46 ---------------------------------------------------------------------------------

def _at25(w, amount="12000.00"):
    c = l1(w, 3, start=date(2027, 3, 1))
    w.clock.set(date(2027, 5, 10))
    v = services.approve_version(version(w, c, effective=date(2027, 3, 16), amount=amount), user=w.c)
    return c, v


def test_at25_backdated_increase_debit_notes(w):
    c, v = _at25(w)
    mar, apr, may = _adjs(c)
    assert [(a.kind, _net(a)) for a in (mar, apr, may)] == [("debit", D("1032.26")), ("debit", D("2000.00")),
                                                           ("debit", D("2000.00"))]
    assert mar.lines[0]["required"] == "11032.26" and mar.supply_start == date(2027, 3, 16)
    assert all(a.document.doc_type == "debit_note" and a.document.status == "draft" for a in (mar, apr, may))
    final = approve_note(w, mar.document, day=date(2027, 5, 10))
    assert (final.total_vat, final.total_gross) == (D("154.84"), D("1187.10"))
    assert sorted((ln.account.code, ln.debit, ln.credit) for ln in final.journal_entry.lines.all()) == [  # B8
        ("1200", D("1187.10"), D("0.00")), ("2200", D("0.00"), D("154.84")), ("4100", D("0.00"), D("1032.26"))]
    run(w, date(2027, 6, 1))
    assert period(c, date(2027, 6, 1)).sales_document.total_net == D("12000.00")


def test_at25_backdated_decrease_credit_notes(w):
    c, v = _at25(w, amount="9000.00")
    assert [(a.kind, _net(a)) for a in _adjs(c)] == [("credit", D("-516.13")), ("credit", D("-1000.00")),
                                                    ("credit", D("-1000.00"))]


def test_at26_no_double_billing_through_recompute_reruns_and_superseding(w):
    c, v2 = _at25(w)
    mar, apr, may = _adjs(c)
    # (a) recompute with no new version creates nothing
    assert services.recompute_corrections(c, user=w.s) == []
    # (b) re-running the months bills nothing and uses no number
    used = sum(DocumentSequence.objects.filter(entity=w.alm).values_list("last_number", flat=True))
    for m in (3, 4, 5):
        r = run(w, date(2027, 5, m))
        assert r.generated_count == 0
    assert sum(DocumentSequence.objects.filter(entity=w.alm).values_list("last_number", flat=True)) == used
    # (c) only the April debit note is issued; then version 3 goes back to 10,000.00 from April
    approve_note(w, apr.document, day=date(2027, 5, 12))
    mar_doc, may_doc = mar.document_id, may.document_id
    w.clock.set(date(2027, 5, 15))
    services.approve_version(version(w, c, effective=date(2027, 4, 1), amount="10000.00"), user=w.c)
    mar.refresh_from_db(), apr.refresh_from_db(), may.refresh_from_db()
    assert (mar.status, may.status, apr.status) == ("superseded", "superseded", "final")
    assert not SalesDocument.objects.filter(pk__in=[mar_doc, may_doc]).exists()  # drafts deleted through invoicing
    new = _adjs(c, status__in=BillingAdjustment.PENDING)
    assert [(a.billing_period.period_start, a.kind, _net(a)) for a in new] == [
        (date(2027, 3, 1), "debit", D("1032.26")), (date(2027, 4, 1), "credit", D("-2000.00"))]
    for adj in new:
        approve_note(w, adj.document, day=date(2027, 5, 16))
    assert [_position(period(c, date(2027, m, 1))) for m in (3, 4, 5)] == [D("11032.26"), D("10000.00"),
                                                                           D("10000.00")]
    assert services.recompute_corrections(c, user=w.s) == []
    for m in (3, 4, 5):  # no period has two invoices
        p = period(c, date(2027, m, 1))
        assert SalesDocument.objects.filter(doc_type="invoice", supply_date=p.period_start, entity=w.alm).count() == 1


def test_at48_submitted_draft_superseded_returned_and_deleted(w):
    c, v2 = _at25(w)
    mar = _adjs(c)[0]
    inv.update_sales_document(mar.document, user=w.s, payment_means_code="30")  # header fields: accepted
    inv.submit_sales_document(mar.document, user=w.s)
    run(w, date(2027, 5, 11))  # read back: the reviewer's submission
    mar.refresh_from_db()
    assert (mar.status, mar.reviewed_by) == ("submitted", w.s)
    doc_pk = mar.document_id
    services.approve_version(version(w, c, effective=date(2027, 3, 1), amount="10500.00"), user=w.c)
    mar.refresh_from_db()
    assert mar.status == "superseded" and not SalesDocument.objects.filter(pk=doc_pk).exists()
    assert doc_pk in w.fake.discarded


# --- AT-28, AT-38, R45, D12 --------------------------------------------------------------------------------------

def test_at28_at38_note_dated_on_its_own_issue_date_in_an_open_period(w):
    c = l1(w, 3)
    w.clock.set(date(2027, 3, 20))
    services.approve_version(version(w, c, kind="termination", effective=date(2027, 1, 15), reason_ar="إنهاء"),
                             user=w.c)
    jan = _adjs(c)[0]
    assert jan.document.adjustment_event_date is None  # D12: no backdated event date
    assert (jan.supply_start, jan.supply_end) == (date(2027, 1, 15), date(2027, 1, 31))
    for name in ("2027-01", "2027-02"):
        FiscalPeriod.objects.filter(entity=w.alm, name=name).update(status="closed")
    final = approve_note(w, jan.document, day=date(2027, 3, 22))
    assert (final.issue_date, final.journal_entry.date) == (date(2027, 3, 22), date(2027, 3, 22))
    assert not getattr(final, "warnings", [])
    feb = _adjs(c)[1]
    FiscalPeriod.objects.filter(entity=w.alm, name="2027-03").update(status="closed")
    w.clock.set(date(2027, 3, 25))
    inv.update_sales_document(feb.document, user=w.s, payment_means_code="30")
    inv.submit_sales_document(feb.document, user=w.s)
    numbers = DocumentSequence.objects.filter(entity=w.alm, doc_type="CN").values_list("last_number", flat=True).first()
    with pytest.raises(InvoicingError) as exc:
        inv.approve_sales_document(SalesDocument.objects.get(pk=feb.document_id), user=w.c)
    assert "closed" in str(exc.value) and "مقفلة" in str(exc.value)
    assert SalesDocument.objects.get(pk=feb.document_id).status == "submitted"
    assert DocumentSequence.objects.filter(entity=w.alm, doc_type="CN").values_list(
        "last_number", flat=True).first() == numbers
    assert FiscalPeriod.objects.get(entity=w.alm, name="2027-01").status == "closed"  # billing reopens nothing


def test_at38_note_approved_a_month_later_is_dated_then(w):
    c = l1(w, 1)
    w.clock.set(date(2027, 3, 20))
    services.approve_version(version(w, c, kind="termination", effective=date(2027, 1, 15), reason_ar="إنهاء"),
                             user=w.c)
    final = approve_note(w, _adjs(c)[0].document, day=date(2027, 4, 20))
    assert (final.issue_date, final.supply_date, final.supply_end_date) == (
        date(2027, 4, 20), date(2027, 1, 15), date(2027, 1, 31))
    assert final.adjustment_event_date is None


# --- AT-29, R47, R55 ---------------------------------------------------------------------------------------

def test_at29_waiting_original_then_drafted_then_rejected_note_recomputed(w):
    c = l1(w, 2)
    run(w, date(2027, 3, 1))  # March issued, ZATCA pending
    mar_inv = period(c, date(2027, 3, 1)).sales_document
    w.clock.set(date(2027, 3, 10))
    services.approve_version(version(w, c, effective=date(2027, 3, 1), amount="10500.00"), user=w.c)
    (mar,) = _adjs(c)
    assert mar.status == "waiting_original" and mar.document is None
    clear(w, mar_inv)
    run(w, date(2027, 3, 11))
    mar.refresh_from_db()
    assert mar.status == "drafted" and mar.document.doc_type == "debit_note"
    inv.update_sales_document(mar.document, user=w.s, payment_means_code="30")
    inv.submit_sales_document(mar.document, user=w.s)
    issued = inv.approve_sales_document(SalesDocument.objects.get(pk=mar.document_id), user=w.c)
    w.zatca.reject(issued)
    run(w, date(2027, 3, 12))
    mar.refresh_from_db()
    assert mar.status == "failed"
    created = services.recompute_corrections(c, user=w.s)
    assert len(created) == 1 and created[0].status == "drafted" and _net(created[0]) == D("500.00")
    rejected = SalesDocument.objects.get(pk=issued.pk)
    assert rejected.status == "rejected" and rejected.number  # keeps its number


# --- AT-30, R48 ---------------------------------------------------------------------------------------------

def test_at30_discard_needs_arabic_reason_and_stays_on_the_report(w):
    c, v2 = _at25(w)
    apr = _adjs(c)[1]
    with pytest.raises(BillingError):
        services.discard_adjustment(apr, user=w.s, reason_ar="")
    doc_pk = apr.document_id
    apr = services.discard_adjustment(apr, user=w.s, reason_ar="شروط العقد لا تسمح بالاسترداد")
    assert apr.status == "discarded" and not SalesDocument.objects.filter(pk=doc_pk).exists()
    report = services.unresolved_differences(w.alm, user=w.s)
    assert [(r["period_start"], r["status"], r["delta"]) for r in report] == [(date(2027, 4, 1), "discarded",
                                                                              D("2000.00"))]
    assert report[0]["status_ar"] == "مستبعد" and report[0]["reason_ar"]
    with pytest.raises(access.AccessDenied):
        services.unresolved_differences(w.alm, user=w.sub_s)
    assert services.recompute_corrections(c, user=w.s) == []  # not re-created for the same version
    again = services.recompute_corrections(c, user=w.s, include_discarded=True)
    assert [(a.billing_period.period_start, a.kind) for a in again] == [(date(2027, 4, 1), "debit")]
    services.discard_adjustment(again[0], user=w.s, reason_ar="مرة أخرى")
    services.approve_version(version(w, c, effective=date(2027, 3, 16), amount="12500.00"), user=w.c)
    assert date(2027, 4, 1) in [a.billing_period.period_start for a in _adjs(c, status="drafted")]


# --- AT-33, AT-35, R44: who may do what ---------------------------------------------------------------------

def test_at33_agents_and_the_system_user_never_review_or_approve_corrections(w, agent):
    c, v2 = _at25(w)
    adj = _adjs(c)[0]
    with pytest.raises(access.AccessDenied):
        services.create_version(c, user=agent, kind="amendment", effective_date=date(2027, 4, 1), reason_ar="س")
    with pytest.raises(access.AccessDenied):
        services.discard_adjustment(adj, user=agent, reason_ar="س")
    with pytest.raises(access.AccessDenied):
        services.discard_adjustment(adj, user=w.sys, reason_ar="س")
    v3 = version(w, c, effective=date(2027, 4, 1), amount="10000.00")
    with pytest.raises(access.AccessDenied):
        services.approve_version(v3, user=agent)
    with pytest.raises(access.AccessDenied):
        services.approve_version(v3, user=w.sys)
    with pytest.raises(access.AccessDenied):
        inv.submit_sales_document(adj.document, user=w.sys)
    inv.update_sales_document(adj.document, user=w.s, payment_means_code="30")
    inv.submit_sales_document(adj.document, user=w.s)
    with pytest.raises(access.AccessDenied):  # the reviewer never approves the note (R44)
        inv.approve_sales_document(SalesDocument.objects.get(pk=adj.document_id), user=w.s)
    with pytest.raises(access.AccessDenied):
        services.recompute_corrections(c, user=w.v)  # a viewer
    assert services.discard_adjustment(_adjs(c)[1], user=w.r, reason_ar="مراجعة").status == "discarded"


def test_at35_correction_notes_are_never_auto_issued(w):
    c, v2 = _at25(w)
    assert all(a.document.status == "draft" for a in _adjs(c))
    run(w, date(2027, 5, 11))
    assert all(SalesDocument.objects.get(pk=a.document_id).status == "draft" for a in _adjs(c))


# --- AT-36, R50: Sharia on versions ---------------------------------------------------------------------------

def test_at36_sharia_checks_on_version_lines(w):
    Account.objects.create(entity=w.alm, code="4950", name_en="Prohibited income (test)", name_ar="إيراد محظور",
                           type="income", sharia_prohibited=True)
    c = l1(w, 2)
    w.clock.set(date(2027, 2, 10))
    v = services.create_version(c, user=w.s, kind="amendment", effective_date=date(2027, 2, 1), reason_ar="خدمة إضافية")
    with pytest.raises(BillingError):
        services.add_line(v, user=w.s, income_type="rent_commercial", amount="100.00", description_ar="رسوم",
                          income_account="4950")
    services.add_line(v, user=w.s, income_type="rent_commercial", amount="100.00", description_ar="رسوم غير متوافقة",
                      income_account="4900")
    services.submit_version(v, user=w.s)
    services.approve_version(v, user=w.c)
    c.refresh_from_db()
    assert c.requires_purification
    (feb,) = _adjs(c)
    assert feb.kind == "debit" and feb.lines[0]["income_account"] == "4900"
    final = approve_note(w, feb.document, day=date(2027, 2, 10))
    assert final.requires_purification
    assert ContractVersion.Status("approved").label.endswith("معتمدة")
    assert BillingAdjustment.Status(feb.status).label.split(" / ")[1]
