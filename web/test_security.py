"""Security headers, CSRF, URL shape, GET-is-read-only and code scans of the web app (web.md R1, R2, R5, R20 to R23;
WT-15, WT-16, WT-19, WT-37)."""
import ast
import re
from pathlib import Path

import pytest
from django.test import Client
from django.urls import URLPattern, get_resolver

from core.access.models import AccessLog
from invoicing.models import SalesDocument
from ledger.models import JournalEntry
from web.models import LoginThrottle, UiPreference

pytestmark = pytest.mark.django_db
WEB = Path(__file__).resolve().parent
TEMPLATES = sorted((WEB / "templates").rglob("*.html"))

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")


def test_wt16_security_headers(w, signed_in):
    client = signed_in(w.acc)
    response = client.get("/e/ALM/")
    assert response["Content-Security-Policy"] == CSP
    assert response["X-Frame-Options"] == "DENY"
    assert response["Referrer-Policy"] == "same-origin"
    assert response["X-Content-Type-Options"] == "nosniff"
    secure = client.get("/login/", secure=True)
    assert secure["Strict-Transport-Security"].startswith("max-age=31536000")


def test_wt16_templates_have_no_inline_script_style_or_handlers():
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", text), path  # every script has src
        assert not re.search(r"<style\b", text), path
        assert not re.search(r"\sstyle\s*=", text), path
        assert not re.search(r"\son[a-z]+\s*=", text), path
        assert "hx-on" not in text, path
        assert not re.search(r"""(?:src|href|action)\s*=\s*["'](?:https?:)?//""", text), path  # no outside origin


def test_wt16_static_css_and_js_reference_no_outside_origin():
    for path in [*(WEB / "static").rglob("*.css"), *(WEB / "static").rglob("*.js")]:
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"url\(\s*['\"]?([^'\")]+)", text):
            assert not re.match(r"(?:https?:)?//", target), (path, target)
        assert "@import" not in text or path.name.startswith("bootstrap"), path


def test_wt15_post_without_csrf_token_is_refused_and_changes_nothing(w):
    client = Client(enforce_csrf_checks=True)
    client.force_login(w.u)
    response = client.post("/switch-entity/", {"entity": "MIN"})
    assert response.status_code == 403
    body = response.content.decode()
    assert "انتهت صلاحية النموذج" in body and "The form expired" in body
    assert not UiPreference.objects.exists()
    assert Client(enforce_csrf_checks=True).post("/login/", {"username": "acc", "password": "x"}).status_code == 403


def test_wt15_no_csrf_exempt_in_web():
    for path in WEB.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        assert "csrf_exempt" not in path.read_text(encoding="utf-8"), path


def _patterns(resolver=None, prefix=""):
    resolver = resolver or get_resolver("web.urls")
    for p in resolver.url_patterns:
        if isinstance(p, URLPattern):
            yield prefix + str(p.pattern)
        else:
            yield from _patterns(p, prefix + str(p.pattern))


def test_wt19_path_parameters_are_entity_codes_and_integer_pks_only():
    params = [m for pattern in _patterns() for m in re.findall(r"<([^>]+)>", pattern)]
    assert params and set(params) <= {"entity:entity_code", "int:pk"}


def _business_rows():
    return (AccessLog.objects.count(), JournalEntry.objects.count(), SalesDocument.objects.count(),
            UiPreference.objects.count(), LoginThrottle.objects.count())


def test_r5_wt19_get_crawl_writes_no_rows_and_puts_no_personal_data_in_urls(w, signed_in):
    client = signed_in(w.acc)
    pages = ["/", "/e/ALM/", "/e/ALM/ledger/entries/", "/e/ALM/sales/documents/",
             f"/e/ALM/ledger/entries/{w.alm_draft.pk}/", f"/e/ALM/sales/documents/{w.alm_person_invoice.pk}/",
             f"/e/ALM/sales/documents/{w.alm_invoice.pk}/", "/e/ALM/ledger/entries/?status=posted"]
    before = _business_rows()
    personal = ("Made Up Person", "شخص مختلق", "Buyer Street")
    for url in pages:
        response = client.get(url)
        assert response.status_code in (200, 302), url
        links = re.findall(r'(?:href|action)="([^"]*)"', response.content.decode())
        for link in [*links, response.get("Location", "")]:
            assert not any(value in link for value in personal), (url, link)
    assert _business_rows() == before


def test_r22_signed_in_responses_are_not_cached(w, signed_in):
    client = signed_in(w.acc)
    for url in ["/e/ALM/", "/e/ALM/ledger/entries/", "/e/NOPE/"]:
        cache = client.get(url)["Cache-Control"]
        assert "no-store" in cache and "private" in cache, url


# --- R1 / R2 / WT-37: thin views, scanned -------------------------------------------------------------------------

THIN_MODULES = ["views.py", "forms.py", "middleware.py", "context_processors.py", "urls.py",
                "templatetags/web_tags.py"]
FORBIDDEN_ATTRS = {"objects", "_base_manager", "_default_manager", "raw", "extra", "update", "save", "delete",
                   "bulk_create", "bulk_update", "get_or_create", "update_or_create", "create", "execute", "cursor"}
ACCESS_ALLOWED = {"has", "entities_for", "AccessDenied"}


def _tree(name):
    return ast.parse((WEB / name).read_text(encoding="utf-8"))


def test_wt37_views_forms_and_tags_never_touch_models_directly():
    for name in THIN_MODULES:
        for node in ast.walk(_tree(name)):
            if isinstance(node, ast.Attribute):
                if node.attr == "save" and isinstance(node.value, ast.Attribute) and node.value.attr == "session":
                    continue  # request.session.save(): the session, not a business model
                assert node.attr not in FORBIDDEN_ATTRS, (name, node.attr, node.lineno)
            if isinstance(node, ast.Name):
                assert node.id not in {"connection", "connections", "RawSQL"}, (name, node.id)


def test_r1_views_use_only_public_services_and_has_from_access():
    tree = _tree("views.py")
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:  # from . import services  ->  web.services
                module = ".".join(filter(None, ["web", module]))
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
    used = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            used.setdefault(aliases[node.value.id], set()).add(node.attr)
    assert used["core.access.services"] <= ACCESS_ALLOWED
    for module in ("ledger.services", "invoicing.services", "web.services"):
        assert all(not attr.startswith("_") for attr in used[module]), module


def test_r2_web_never_uses_system_users():
    for name in [*THIN_MODULES, "services.py"]:
        assert "system_user" not in (WEB / name).read_text(encoding="utf-8"), name


def test_r34_no_float_in_the_web_code_path():
    for name in [*THIN_MODULES, "services.py"]:
        source = (WEB / name).read_text(encoding="utf-8")
        assert "FloatField" not in source and "floatformat" not in source, name
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "float":
                assert name == "services.py", (name, node.lineno)  # only the Accept-Language q value
    for path in TEMPLATES:
        assert "floatformat" not in path.read_text(encoding="utf-8"), path


# --- review item 3: no MFA yet, so a production deploy check refuses password-only sign-in ----------------------

def test_web_e001_deploy_check_refuses_password_only_login_outside_debug(settings):
    from web.checks import password_only_login

    settings.DEBUG, settings.WEB_ALLOW_PASSWORD_ONLY_LOGIN = False, False
    (error,) = password_only_login()
    assert error.id == "web.E001" and error.is_serious()
    settings.WEB_ALLOW_PASSWORD_ONLY_LOGIN = True  # explicit opt-in (CI deploy-check only)
    assert password_only_login() == []
    settings.DEBUG, settings.WEB_ALLOW_PASSWORD_ONLY_LOGIN = True, False
    assert password_only_login() == []


def test_web_e001_is_a_deploy_check_only_and_off_by_default():
    from django.core.checks import registry

    assert "password_only_login" in {c.__name__ for c in registry.registry.get_checks(include_deployment_checks=True)}
    assert "password_only_login" not in {c.__name__ for c in registry.registry.get_checks()}  # migrate, jobs: no
    settings_py = (WEB.parent / "config/settings.py").read_text(encoding="utf-8")
    assert 'os.environ.get("DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN", "0") == "1"' in settings_py
    compose = (WEB.parent / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN: ${DJANGO_WEB_ALLOW_PASSWORD_ONLY_LOGIN:-0}" in compose
