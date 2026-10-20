"""Entity separation and permissions in the web UI (web.md R2 to R4, R6, R9 to R12, R25; WT-1 to WT-3, WT-5,
WT-11, WT-33, WT-34). Fixture data only (web/conftest.py)."""
from importlib import import_module

import pytest
from django.contrib.auth import SESSION_KEY
from django.test import Client

from core.access.models import AccessLog
from web.models import UiPreference

pytestmark = pytest.mark.django_db

SECRET = "SUB-ONLY-SECRET"  # appears only in SUB's records (web/conftest.py)


def entity_urls(code):
    return [f"/e/{code}/", f"/e/{code}/ledger/entries/", f"/e/{code}/sales/documents/"]


def detail_urls(code, w):
    return [f"/e/{code}/ledger/entries/{w.sub_entry.pk}/", f"/e/{code}/sales/documents/{w.sub_invoice.pk}/"]


def test_unauthenticated_requests_are_redirected_to_sign_in(w, client):
    for url in ["/", *entity_urls("ALM"), f"/e/ALM/ledger/entries/{w.alm_draft.pk}/",
                f"/e/ALM/sales/documents/{w.alm_invoice.pk}/"]:
        response = client.get(url)
        assert response.status_code == 302, url
        assert response["Location"].startswith("/login/?next="), url


def test_unauthenticated_switch_is_redirected_and_changes_nothing(w, client):
    response = client.post("/switch-entity/", {"entity": "ALM"})
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    assert not UiPreference.objects.exists()


def test_wt1_lists_show_only_the_url_entity_and_other_entities_are_404(w, signed_in):
    client = signed_in(w.acc)
    entries = client.get("/e/ALM/ledger/entries/")
    assert entries.status_code == 200
    body = entries.content.decode()
    assert "ALM rent accrual" in body and "ALM posted entry" in body and SECRET not in body
    sales = client.get("/e/ALM/sales/documents/")
    body = sales.content.decode()
    assert sales.status_code == 200 and "C001" in body and "S001" not in body and SECRET not in body
    for url in entity_urls("SUB") + entity_urls("MIN") + entity_urls("NOPE"):
        assert client.get(url).status_code == 404, url


def test_wt2_a_record_of_another_entity_is_404_with_the_same_page_and_no_data(w, signed_in):
    client = signed_in(w.acc)
    bodies = set()
    for url in detail_urls("ALM", w) + detail_urls("SUB", w) + ["/e/ALM/ledger/entries/999999/"]:
        response = client.get(url)
        assert response.status_code == 404, url
        body = response.content.decode()
        assert SECRET not in body and "S001" not in body
        assert "غير موجود" in body and "Not found" in body  # bilingual (R25)
        bodies.add(body.replace(url, ""))
    assert len(bodies) == 1  # the same page whether the record exists elsewhere or not at all


def test_wt2_the_owner_sees_the_record_only_under_its_own_entity(w, signed_in):
    client = signed_in(w.u)
    assert client.get(f"/e/SUB/ledger/entries/{w.sub_entry.pk}/").status_code == 200
    assert client.get(f"/e/MIN/ledger/entries/{w.sub_entry.pk}/").status_code == 404  # R4: entity in the URL
    assert client.get(f"/e/SUB/ledger/entries/{w.alm_draft.pk}/").status_code == 404


def switcher_codes(response):
    import re

    select = re.search(r'<select[^>]*name="entity"[^>]*>(.*?)</select>', response.content.decode(), re.S)
    return re.findall(r'<option value="([^"]+)"', select.group(1)) if select else []


def test_wt3_switcher_lists_exactly_the_visible_entities(w, signed_in, client):
    g = signed_in(w.g).get("/e/ALM/")
    assert switcher_codes(g) == ["ALM", "INACT", "PART", "SUB"]  # group role covers full children, not MIN (R6)
    assert "غير نشط" in g.content.decode()  # INACT marked inactive (Arabic UI)
    client.post("/logout/")
    u = signed_in(w.u).get("/e/SUB/")
    assert switcher_codes(u) == ["MIN", "SUB"]


def test_wt3_switching_opens_the_dashboard_and_carries_nothing_over(w, signed_in):
    client = signed_in(w.u)
    response = client.post("/switch-entity/", {"entity": "MIN", "description": "half-filled", "status": "draft",
                                               "next": "/e/SUB/ledger/entries/?status=draft"})
    assert response.status_code == 302 and response["Location"] == "/e/MIN/"  # R11: no query, no form data
    assert UiPreference.objects.get(user=w.u).last_entity == w.min
    assert client.get("/").headers["Location"] == "/e/MIN/"  # R10: remembered


def test_switch_to_an_entity_outside_the_users_set_is_404_and_remembers_nothing(w, signed_in):
    client = signed_in(w.u)
    assert client.post("/switch-entity/", {"entity": "ALM"}).status_code == 404
    assert not UiPreference.objects.filter(user=w.u, last_entity__isnull=False).exists()
    assert client.get("/switch-entity/").status_code == 405  # a switch is a POST (R5)


def test_r10_a_remembered_entity_no_longer_visible_is_not_used(w, signed_in):
    UiPreference.objects.create(user=w.acc, last_entity=w.sub)  # e.g. a membership that has ended since
    response = signed_in(w.acc).get("/")
    assert response.headers["Location"] == "/e/ALM/"


def test_wt5_page_without_the_view_right_is_a_bilingual_403_and_hidden_from_the_menu(w, signed_in):
    client = signed_in(w.hr)  # hr_officer: ledger.view, no invoicing.view
    before = AccessLog.objects.filter(event="permission_denied").count()
    response = client.get("/e/ALM/sales/documents/")
    assert response.status_code == 403
    body = response.content.decode()
    assert "Not allowed" in body and "غير مسموح" in body
    assert "You do not have the right" in body and "ليست لديك صلاحية" in body  # the access layer's reason
    assert AccessLog.objects.filter(event="permission_denied").count() == before + 1
    assert client.get(f"/e/ALM/sales/documents/{w.alm_invoice.pk}/").status_code == 403
    dashboard = client.get("/e/ALM/").content.decode()
    assert "/e/ALM/sales/documents/" not in dashboard and "/e/ALM/ledger/entries/" in dashboard


def test_dashboard_cards_only_for_modules_the_user_may_view(w, signed_in):
    from core.access.testing import make_user, member
    from web.conftest import PASSWORD

    chairman = make_user("chair", language="en")
    member(chairman, w.alm, "chairman")  # holds only the Sharia second approval: no module card (R38)
    chairman.set_password(PASSWORD)
    chairman.save()
    body = signed_in(chairman).get("/e/ALM/").content.decode()
    assert "Your role has no overview cards in this entity." in body
    assert "/ledger/entries/" not in body and "/sales/documents/" not in body


def test_wt33_inactive_entity_pages_are_readable_and_marked(w, signed_in):
    client = signed_in(w.inact_acc)
    for url in entity_urls("INACT"):
        response = client.get(url)
        assert response.status_code == 200, url
        assert "هذا الكيان غير نشط" in response.content.decode()


def test_wt34_superuser_without_memberships_has_no_entity(w, signed_in):
    client = signed_in(w.su)
    response = client.get("/")
    assert response.status_code == 200
    body = response.content.decode()
    assert "لا يوجد كيان متاح" in body and switcher_codes(response) == []
    for url in entity_urls("ALM"):
        assert client.get(url).status_code == 404


@pytest.mark.parametrize("who", ["agent", "billing"])
def test_wt11_agent_and_system_users_cannot_sign_in_generic_message(w, client, who):
    user = getattr(w, who)
    user.set_password("known-only-to-this-test")
    user.save(update_fields=["password"])
    response = client.post("/login/", {"username": user.username, "password": "known-only-to-this-test"})
    assert response.status_code == 200
    body = response.content.decode()
    assert "اسم المستخدم أو كلمة المرور غير صحيحة." in body
    assert SESSION_KEY not in client.session
    wrong = client.post("/login/", {"username": "nobody-at-all", "password": "x"}).content.decode()
    assert "اسم المستخدم أو كلمة المرور غير صحيحة." in wrong  # the same answer for unknown users


def forged_client(user):
    engine = import_module("django.contrib.sessions.backends.db")
    session = engine.SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session["_auth_user_backend"] = "core.access.backends.HumanModelBackend"
    session["_auth_user_hash"] = user.get_session_auth_hash()
    session.save()
    client = Client()
    client.cookies["sessionid"] = session.session_key
    return client, session.session_key


def test_wt11_a_forged_agent_session_is_flushed_and_sent_to_sign_in(w):
    client, key = forged_client(w.agent)
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    engine = import_module("django.contrib.sessions.backends.db")
    assert not engine.SessionStore().exists(key)


def test_r12_a_deactivated_user_is_signed_out_at_the_next_request(w, signed_in):
    client = signed_in(w.acc)
    assert client.get("/e/ALM/").status_code == 200
    w.acc.is_active = False
    w.acc.save(update_fields=["is_active"])
    response = client.get("/e/ALM/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    assert SESSION_KEY not in client.session
