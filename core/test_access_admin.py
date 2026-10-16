"""Admin follows memberships, not Django permissions (access.md R24, AT-22) and logs grants (R27)."""
from datetime import date
from types import SimpleNamespace

import pytest

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


def test_admin_post_action_uses_grants(client, w):
    client.force_login(w.clerk)  # creator: no ledger.post_entry right
    client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [w.alm_draft.pk]})
    assert JournalEntry.objects.get(pk=w.alm_draft.pk).status == "draft"
    client.force_login(w.cfo)
    client.post("/admin/ledger/journalentry/", {"action": "post_selected", "_selected_action": [w.alm_draft.pk]})
    posted = JournalEntry.objects.get(pk=w.alm_draft.pk)
    assert posted.status == "posted" and posted.posted_by == w.cfo


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
    assert resp.status_code == 200 and "themselves" in resp.content.decode()
    resp = client.post("/admin/core/membership/add/", _grant_form(make_user(), w.sub, "viewer"))
    assert resp.status_code == 200  # SUB is not a choice for this access admin
    resp = client.post("/admin/core/membership/add/", _grant_form(make_user(), w.alm, "system_billing"))
    assert resp.status_code == 200
    assert not Membership.objects.filter(granted_by=w.aa).exists()


def test_access_log_admin_is_read_only(client, w):
    client.force_login(w.aa)
    assert client.get("/admin/core/accesslog/").status_code == 200
    assert client.get("/admin/core/accesslog/add/").status_code == 403


def test_admin_line_edit_keeps_functional_txn_amount_in_sync(w):
    """ledger-fx R6: editing a functional line's amount in admin also updates its transaction amount."""
    from django.forms import modelform_factory

    from ledger.admin import JournalLineForm, JournalLineInline
    from ledger.models import JournalLine

    line = JournalLine.objects.filter(entry=w.alm_draft, debit__gt=0).get()
    editable = [f for f in JournalLineInline.fields if f not in JournalLineInline.readonly_fields]
    Form = modelform_factory(JournalLine, form=JournalLineForm, fields=editable)
    form = Form(
        data={"account": line.account_id, "description": "", "debit": "12.00", "credit": "0", "counterparty_entity": ""},
        instance=line,
    )
    assert form.is_valid(), form.errors
    form.save()
    line.refresh_from_db()
    assert (line.debit, line.txn_amount) == (12, 12)


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
