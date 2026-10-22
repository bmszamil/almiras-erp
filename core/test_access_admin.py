"""Admin follows memberships, not Django permissions (access.md R24, AT-22) and logs grants (R27)."""
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from core.access.models import AccessLog, Membership, Role
from core.access.testing import make_user, member
from core.models import Entity
from ledger import services as ledger
from ledger.models import JournalEntry
from ledger.services_setup import create_monthly_periods, install_chart_of_accounts

pytestmark = pytest.mark.django_db

CASH = [{"account": "1100", "debit": 10}, {"account": "3100", "credit": 10}]


def staff(username, superuser=False):
    u = make_user(username)
    u.is_staff = True
    u.is_superuser = superuser
    u.save()
    return u


@pytest.fixture
def w():
    alm = Entity.objects.create(code="ALM", name_en="ALM", name_ar="كيان", consolidation="parent")
    sub = Entity.objects.create(code="SUB", name_en="SUB", name_ar="كيان فرعي", parent=alm)
    for e in (alm, sub):
        install_chart_of_accounts(e)
        create_monthly_periods(e, 2027)
    clerk, cfo, root, aa = staff("clerk"), staff("cfo"), staff("root", superuser=True), staff("aa")
    member(clerk, alm, "accountant")
    member(cfo, alm, "approver")
    member(aa, alm, "access_admin")
    sub_clerk = make_user("sub.clerk")  # no admin access; drafts SUB's entry (ledger.create_entry in SUB)
    member(sub_clerk, sub, "accountant")
    alm_draft = ledger.create_entry(alm, date(2027, 1, 5), "ALM draft", CASH, user=clerk)
    sub_draft = ledger.create_entry(sub, date(2027, 1, 5), "SUB draft", CASH, user=sub_clerk)
    return SimpleNamespace(alm=alm, sub=sub, clerk=clerk, cfo=cfo, root=root, aa=aa, alm_draft=alm_draft, sub_draft=sub_draft)


def test_at22_superuser_without_membership_sees_and_changes_nothing(client, w):
    client.force_login(w.root)
    assert client.get("/admin/ledger/journalentry/").status_code == 403
    assert client.get(f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/").status_code in (302, 403)
    assert client.get("/admin/core/membership/").status_code == 403
    resp = client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [w.alm_draft.pk]})
    assert resp.status_code == 403
    assert JournalEntry.objects.get(pk=w.alm_draft.pk).status == "draft"


def test_r2_admin_changelist_scoped_and_other_entity_not_found(client, w):
    client.force_login(w.clerk)
    resp = client.get("/admin/ledger/journalentry/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "ALM draft" in body and "SUB draft" not in body
    # Out-of-scope object: admin answers as not found (redirect with "doesn't exist"), never shows it.
    resp = client.get(f"/admin/ledger/journalentry/{w.sub_draft.pk}/change/", follow=True)
    assert "SUB draft" not in resp.content.decode()


def test_at22_status_is_read_only_in_admin(client, w):
    client.force_login(w.clerk)
    url = f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/"
    resp = client.get(url)
    assert resp.status_code == 200 and 'name="status"' not in resp.content.decode()


def _step_up_code(user):
    """A confirmed authenticator for ``user`` (enrolled here if needed) and a fresh code for the next time step."""
    import pyotp

    from web import mfa
    from web.models import MfaDevice

    if mfa.device_of(user) is None:
        device = mfa.start_enrolment(user)
        mfa.confirm_enrolment(user, pyotp.TOTP(device.secret).now())
    secret = MfaDevice.objects.get(user=user).secret
    return pyotp.TOTP(secret).at(timezone.now() + timedelta(seconds=30))


def _post_action(client, user, pk, action="post_selected"):
    """The admin action as an approver runs it: confirmed with a step-up code (web.md R15)."""
    return client.post("/admin/ledger/journalentry/", {"action": action, "_selected_action": [pk],
                                                       "mfa_confirm": "1", "mfa_code": _step_up_code(user)})


def test_admin_post_action_uses_grants(client, w):
    client.force_login(w.clerk)  # creator: no ledger.post_entry right
    client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [w.alm_draft.pk]})
    assert JournalEntry.objects.get(pk=w.alm_draft.pk).status == "draft"
    client.force_login(w.cfo)
    _post_action(client, w.cfo, w.alm_draft.pk)
    posted = JournalEntry.objects.get(pk=w.alm_draft.pk)
    assert posted.status == "posted" and posted.posted_by == w.cfo


def test_r15_admin_posting_needs_a_step_up_code(client, w):
    """Final verification of 7a104f1, item 3: like the inbox, admin posting asks for a fresh code first."""
    from core.access.models import AccessLog

    client.force_login(w.cfo)
    page = client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [w.alm_draft.pk]})
    assert page.status_code == 200 and 'name="mfa_code"' in page.content.decode()
    assert JournalEntry.objects.get(pk=w.alm_draft.pk).status == "draft"  # nothing posted without the code
    _step_up_code(w.cfo)  # enrol
    wrong = client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [w.alm_draft.pk],
                                                        "mfa_confirm": "1", "mfa_code": "000000"})
    assert wrong.status_code == 400 and JournalEntry.objects.get(pk=w.alm_draft.pk).status == "draft"
    assert AccessLog.objects.filter(event="mfa_verify_failed", actor=w.cfo, details__purpose="step_up").exists()


def _grant_form(user, entity, role, reason="Hired as AP clerk"):
    return {
        "user": user.pk, "entity": entity.pk, "role": Role.objects.get(code=role).pk,
        "valid_from_0": "2026-01-01", "valid_from_1": "00:00:00", "valid_to_0": "", "valid_to_1": "", "reason": reason,
    }


def test_membership_admin_grant_and_revoke_are_logged(client, w):
    b = make_user("b")
    client.force_login(w.aa)
    resp = client.post("/admin/core/membership/add/", _grant_form(b, w.alm, "payables_clerk"))
    assert resp.status_code == 302, resp.content.decode()[:2000]
    m = Membership.objects.get(user=b)
    assert m.granted_by == w.aa
    assert AccessLog.objects.filter(event="membership_granted", membership=m, actor=w.aa).exists()
    resp = client.post(f"/admin/core/membership/{m.pk}/change/", {"revoke_reason": "Contract ended"})
    assert resp.status_code == 302
    m.refresh_from_db()
    assert m.revoked_by == w.aa and m.revoke_reason == "Contract ended"
    assert AccessLog.objects.filter(event="membership_revoked", membership=m, actor=w.aa).exists()
    assert client.post(f"/admin/core/membership/{m.pk}/delete/", {"post": "yes"}).status_code == 403
    assert Membership.objects.filter(pk=m.pk).exists()


def test_membership_admin_refuses_self_grant_and_bad_roles(client, w):
    client.force_login(w.aa)
    resp = client.post("/admin/core/membership/add/", _grant_form(w.aa, w.alm, "viewer"))
    assert resp.status_code == 200 and "Select a valid choice" in resp.content.decode()  # not even listed
    resp = client.post("/admin/core/membership/add/", _grant_form(make_user(), w.sub, "viewer"))
    assert resp.status_code == 200  # SUB is not a choice for this access admin
    resp = client.post("/admin/core/membership/add/", _grant_form(make_user(), w.alm, "system_billing"))
    assert resp.status_code == 200
    assert not Membership.objects.filter(granted_by=w.aa).exists()


def test_access_log_admin_is_read_only(client, w):
    client.force_login(w.aa)
    assert client.get("/admin/core/accesslog/").status_code == 200
    assert client.get("/admin/core/accesslog/add/").status_code == 403


def _entry_form(entry, lines, **header):
    data = {
        "date": header.get("date", entry.date.isoformat()), "description": header.get("description", entry.description),
        "reference": header.get("reference", entry.reference),
        "lines-TOTAL_FORMS": str(len(lines)), "lines-INITIAL_FORMS": str(sum(1 for l in lines if l.get("id"))),
        "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
    }
    for i, line in enumerate(lines):
        for key, value in line.items():
            data[f"lines-{i}-{key}"] = "" if value is None else str(value)
        data.setdefault(f"lines-{i}-entry", str(entry.pk))
    return data


def _line_rows(entry):
    from ledger.models import JournalLine

    return [
        {"id": l.pk, "account": l.account_id, "description": l.description, "debit": l.debit, "credit": l.credit,
         "counterparty_entity": l.counterparty_entity_id}
        for l in JournalLine.objects.filter(entry=entry).order_by("pk")
    ]


def test_admin_draft_edit_goes_through_the_service(client, w):
    """ledger-fx R6 and access R14: an admin edit rebuilds the lines through update_draft (txn = amount)
    and records the editor as last modifier."""
    from ledger.models import JournalLine

    client.force_login(w.clerk)
    rows = _line_rows(w.alm_draft)
    for row in rows:
        row["debit"], row["credit"] = ("12.00", "0") if float(row["debit"]) else ("0", "12.00")
    resp = client.post(f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/", _entry_form(w.alm_draft, rows))
    assert resp.status_code == 302, resp.content.decode()[:3000]
    entry = JournalEntry.objects.get(pk=w.alm_draft.pk)
    assert entry.modified_by == w.clerk and entry.modified_at is not None
    assert sorted((l.debit, l.credit, l.txn_amount) for l in JournalLine.objects.filter(entry=entry)) == [(0, 12, 12), (12, 0, 12)]


def test_admin_cannot_book_another_entitys_account(client, w):
    """Account choices are the entry's own accounts; a crafted POST with a SUB account is refused (rule 4)."""
    from ledger.models import Account, JournalLine

    client.force_login(w.clerk)
    rows = _line_rows(w.alm_draft)
    sub_cash = Account.objects.get(entity=w.sub, code="1100")
    rows[0]["account"] = sub_cash.pk
    before = sorted(JournalLine.objects.filter(entry=w.alm_draft).values_list("account_id", flat=True))
    resp = client.post(f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/", _entry_form(w.alm_draft, rows))
    assert resp.status_code == 200 and "Select a valid choice" in resp.content.decode()
    assert sorted(JournalLine.objects.filter(entry=w.alm_draft).values_list("account_id", flat=True)) == before
    form_html = client.get(f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/").content.decode()
    assert f'value="{sub_cash.pk}"' not in form_html


def test_admin_service_refusal_saves_nothing(client, w):
    """A service refusal rolls the whole admin save back and shows the reason; nothing is half-saved."""
    from ledger.models import Account, JournalLine

    client.force_login(w.clerk)
    rows = _line_rows(w.alm_draft)
    rows[0]["description"] = "changed"
    with pytest.MonkeyPatch.context() as mp:
        from ledger import services as ledger_services

        def refuse(*a, **k):
            raise ledger_services.LedgerError("refused for the test")

        mp.setattr(ledger_services, "update_draft", refuse)
        resp = client.post(f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/", _entry_form(w.alm_draft, rows), follow=True)
    assert "refused for the test" in resp.content.decode()
    assert not JournalLine.objects.filter(entry=w.alm_draft, description="changed").exists()
    assert JournalEntry.objects.get(pk=w.alm_draft.pk).modified_by is None


def test_admin_add_creates_the_draft_through_create_entry(client, w):
    from ledger.models import Account

    client.force_login(w.clerk)
    data = {
        "entity": w.alm.pk, "date": "2027-01-07", "description": "Admin draft", "reference": "",
        "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "0", "lines-MAX_NUM_FORMS": "1000",
        "lines-0-account": Account.objects.get(entity=w.alm, code="1100").pk, "lines-0-debit": "5", "lines-0-credit": "0",
        "lines-1-account": Account.objects.get(entity=w.alm, code="3100").pk, "lines-1-debit": "0", "lines-1-credit": "5",
    }
    resp = client.post("/admin/ledger/journalentry/add/", data)
    assert resp.status_code == 302, resp.content.decode()[:3000]
    entry = JournalEntry.objects.get(description="Admin draft")
    assert (entry.entity, entry.created_by, entry.source, entry.status, entry.lines.count()) == (w.alm, w.clerk, "human", "draft", 2)
    data["entity"] = w.sub.pk  # the clerk has no ledger.create_entry in SUB
    resp = client.post("/admin/ledger/journalentry/add/", {**data, "description": "SUB attempt"})
    assert not JournalEntry.objects.filter(description="SUB attempt").exists()


def test_admin_posted_and_foreign_currency_entries_are_read_only(client, w):
    from ledger import services as ledger_services

    client.force_login(w.cfo)
    _post_action(client, w.cfo, w.alm_draft.pk)
    assert JournalEntry.objects.get(pk=w.alm_draft.pk).status == "posted"
    client.force_login(w.clerk)
    page = client.get(f"/admin/ledger/journalentry/{w.alm_draft.pk}/change/").content.decode()
    assert 'name="lines-0-debit"' not in page and 'name="description"' not in page
    from ledger.models import ExchangeRate

    ExchangeRate.objects.create(from_currency_id="USD", to_currency_id="SAR", date=date(2027, 1, 5), rate="3.75", created_by=w.cfo)
    usd = ledger_services.create_entry(
        w.alm, date(2027, 1, 5), "USD draft",
        [{"account": "1100", "currency": "USD", "txn_debit": 10}, {"account": "3100", "currency": "USD", "txn_credit": 10}],
        user=w.clerk,
    )
    page = client.get(f"/admin/ledger/journalentry/{usd.pk}/change/").content.decode()
    assert 'name="lines-0-debit"' not in page


def test_rate_admin_lists_only_visible_rates_and_is_read_only(client, w):
    from decimal import Decimal

    from ledger.models import ExchangeRate

    ExchangeRate.objects.create(from_currency_id="EUR", to_currency_id="SAR", date=date(2027, 1, 5), rate=Decimal("4.1"), created_by=w.cfo)
    ExchangeRate.objects.create(entity=w.sub, from_currency_id="EUR", to_currency_id="SAR", date=date(2027, 1, 5),
                                rate=Decimal("4.2"), created_by=w.cfo)
    client.force_login(w.clerk)
    body = client.get("/admin/ledger/exchangerate/").content.decode()
    assert "4.1000000000" in body and "4.2000000000" not in body
    assert client.get("/admin/ledger/exchangerate/add/").status_code == 403
    client.force_login(w.root)
    assert client.get("/admin/ledger/exchangerate/").status_code == 403
