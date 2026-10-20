"""Views of the web UI, slice 1 (docs/specs/web.md "As built: slice 1").

Thin by rule (R1 to R4): each view parses input, calls the owning module's service with ``request.user`` and renders
the result. Rights are checked by those services (``require`` / ``scope``); ``core.access.services.has`` is used only
to decide which dashboard cards and menu items to show (R6: hints only). Every page is under ``/e/<entity code>/``
and the entity is resolved only among the entities the user may see (R4, R10); anything else is 404.
Slice 1 is read-only: the only POSTs are sign-in, sign-out, language and entity switch.
"""
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from core.access import services as access_services
from core.access.services import AccessDenied
from invoicing import services as invoicing_services
from invoicing.models import SalesDocument
from ledger import services as ledger_services

from . import services
from .forms import JournalEntryFilterForm, LoginForm, SalesDocumentFilterForm
from .middleware import start_session
from .texts import both, text

PAGE_SIZE = 50  # R37


def _methods(*allowed):
    """Like ``require_http_methods``, but the 405 page is the bilingual one (R5, R17, R25)."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method not in allowed:
                response = _error(request, 405)
                response["Allow"] = ", ".join(allowed)
                return response
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


require_GET = _methods("GET", "HEAD")
require_POST = _methods("POST")


def _safe_next(request, value):
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()},
                                                 require_https=request.is_secure()):
        return value
    return None


def _client_ip(request):
    return request.META.get("REMOTE_ADDR", "")


# --- sign-in, sign-out, language (R12, R17, R18, R26, R27) -------------------------------------------------------

@never_cache
@csrf_protect
@_methods("GET", "HEAD", "POST")
def login_view(request):
    lang = request.web_lang
    next_url = _safe_next(request, request.POST.get("next") or request.GET.get("next"))
    if request.user.is_authenticated and request.method == "GET":
        return redirect(next_url or "web:home")
    form = LoginForm(request.POST or None, lang=lang)
    error = None
    if request.method == "POST":
        username = request.POST.get("username", "")
        ip = _client_ip(request)
        if services.login_locked(username, ip):
            return _error(request, 429)
        if form.is_valid():
            user = authenticate(request, username=form.cleaned_data["username"],
                                password=form.cleaned_data["password"])
            if user is not None and services.is_web_user(user):
                login(request, user)  # rotates the session key (R17)
                start_session(request)
                services.clear_login_failures(username, ip)
                services.log_login(user)
                return HttpResponseRedirect(next_url or reverse("web:home"))
            if services.record_login_failure(username, ip):
                return _error(request, 429)
            error = text("login_failed", lang)  # same answer for unknown, agent, system and inactive users (R12)
    return render(request, "web/login.html", {"form": form, "error": error, "next": next_url or ""})


@require_POST
def logout_view(request):
    """Sign-out is a POST only (R17); GET is 405."""
    if request.user.is_authenticated:
        services.log_logout(request.user)
    logout(request)
    return redirect("web:login")


@require_POST
def language_view(request):
    """The language switch (R27): POST, saved to the profile when signed in, and to a cookie for the sign-in page."""
    language = request.POST.get("language", "")
    if language not in services.LANGUAGES:
        return _error(request, 400)
    services.set_language(request.user, language)
    response = HttpResponseRedirect(_safe_next(request, request.POST.get("next")) or reverse("web:home"))
    response.set_cookie(services.LANGUAGE_COOKIE, language, max_age=365 * 24 * 3600, secure=settings.SESSION_COOKIE_SECURE,
                        httponly=True, samesite="Lax")
    return response


# --- entity pages ----------------------------------------------------------------------------------------------

def entity_page(view):
    """Signed in, then the URL's entity resolved among the visible ones (R4, R10), else 404."""

    @login_required
    @wraps(view)
    def wrapper(request, entity_code, *args, **kwargs):
        entities = services.switcher_entities(request.user)
        entity = services.entity_from(entities, entity_code)
        request.web_entity = entity
        request.web_entities = entities
        return view(request, entity, *args, **kwargs)

    return wrapper


def _page(request, template, entity, context):
    user = request.user
    nav = {
        "ledger": access_services.has(user, entity, "ledger.view"),
        "sales": access_services.has(user, entity, "invoicing.view"),
    }
    return render(request, template, {
        "entity": entity, "entities": request.web_entities, "nav": nav, **context,
    })


@require_GET
@login_required
def home(request):
    """The start page (R10): the remembered entity, else the first visible one, else "no access"."""
    entities = services.switcher_entities(request.user)
    entity = services.start_entity(request.user, entities)
    if entity is None:
        return render(request, "web/no_access.html", {"entities": []})
    return redirect("web:dashboard", entity_code=entity.code)


@require_POST
@login_required
def switch_entity(request):
    """The entity switcher (R9 to R11): opens the new entity's dashboard; nothing carries over."""
    entity = services.remember_entity(request.user, request.POST.get("entity", ""))
    return redirect("web:dashboard", entity_code=entity.code)


@require_GET
@entity_page
def dashboard(request, entity):
    """Cards only for modules the user may view in this entity (R38); counts come from the modules' services."""
    user = request.user
    cards = []
    if access_services.has(user, entity, "ledger.view"):
        counts = ledger_services.entry_counts(entity, user=user)
        cards.append({"key": "ledger", "title": "card_ledger", "empty": "empty_ledger", "kind": "je",
                      "url": reverse("web:journal_entries", args=[entity.code]),
                      "rows": [{"value": k, "count": v} for k, v in counts.items()],
                      "total": sum(counts.values())})
    if access_services.has(user, entity, "invoicing.view"):
        counts = invoicing_services.sales_document_counts(entity, user=user)
        labels = dict(SalesDocument.Status.choices)
        cards.append({"key": "sales", "title": "card_sales", "empty": "empty_sales", "kind": "sd",
                      "url": reverse("web:sales_documents", args=[entity.code]),
                      "rows": [{"value": k, "count": v, "label": labels[k]} for k, v in counts.items()],
                      "total": sum(counts.values())})
    return _page(request, "web/dashboard.html", entity, {"cards": cards, "active": "dashboard"})


def _listing(request, form, rows):
    paginator = Paginator(rows, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    invalid = form.is_bound and bool(form.errors)
    return {"form": form, "page": page, "paginator": paginator, "invalid_filters": invalid,
            "filtered": bool(form.filters()) if form.is_bound else False}


@require_GET
@entity_page
def journal_entries(request, entity):
    form = JournalEntryFilterForm(request.GET or None, lang=request.web_lang)
    rows = ledger_services.journal_entries(entity, user=request.user, **form.filters())
    return _page(request, "web/journal_entries.html", entity, {**_listing(request, form, rows), "active": "ledger"})


@require_GET
@entity_page
def journal_entry(request, entity, pk):
    entry = ledger_services.journal_entry(entity, pk, user=request.user)
    lines = list(entry.lines.all())
    totals = {"debit": sum((line.debit for line in lines), Decimal("0")),
              "credit": sum((line.credit for line in lines), Decimal("0"))}
    return _page(request, "web/journal_entry.html", entity,
                 {"entry": entry, "lines": lines, "totals": totals, "active": "ledger"})


@require_GET
@entity_page
def sales_documents(request, entity):
    form = SalesDocumentFilterForm(request.GET or None, lang=request.web_lang)
    rows = invoicing_services.list_sales_documents(entity, user=request.user, **form.filters())
    listing = _listing(request, form, rows)
    for doc in listing["page"].object_list:
        doc.customer_display = invoicing_services.party_list_name(doc.customer, request.web_lang)
        doc.totals_ready = doc.status in invoicing_services.PRICED_STATUSES
    return _page(request, "web/sales_documents.html", entity, {**listing, "active": "sales"})


@require_GET
@entity_page
def sales_document(request, entity, pk):
    detail = invoicing_services.sales_document_detail(entity, pk, user=request.user)
    return _page(request, "web/sales_document.html", entity, {**detail, "active": "sales"})


# --- error pages (R25): bilingual, no stack trace, nothing about other entities ----------------------------------

_ERRORS = {
    400: ("error_400_title", "error_400_body"),
    403: ("error_403_title", "error_403_body"),
    404: ("error_404_title", "error_404_body"),
    405: ("error_405_title", "error_405_body"),
    429: ("error_429_title", "error_429_body"),
    500: ("error_500_title", "error_500_body"),
}


def _error(request, code, *, body_key=None, detail=None):
    title_key, default_body = _ERRORS[code]
    lang = getattr(request, "web_lang", "ar")
    context = {"code": code, "title": both(title_key), "body": both(body_key or default_body), "detail": detail,
               "home": both("go_home"),
               "lang": lang}
    return render(request, "web/error.html", context, status=code)


def bad_request(request, exception=None):
    return _error(request, 400)


def permission_denied(request, exception=None):
    """403 (R6): the access layer's own bilingual reason when it gave one (AccessDenied.message_en / _ar)."""
    detail = (exception.message_en, exception.message_ar) if isinstance(exception, AccessDenied) else None
    return _error(request, 403, detail=detail)


def page_not_found(request, exception=None):
    return _error(request, 404)


def server_error(request):
    """500: rendered without the database or the user, so it works when they are the problem."""
    from django.template.loader import render_to_string
    from django.http import HttpResponseServerError

    html = render_to_string("web/error.html", {"code": 500, "title": both("error_500_title"),
                                               "body": both("error_500_body"), "lang": "ar", "bare": True})
    return HttpResponseServerError(html)


def csrf_failure(request, reason=""):
    return _error(request, 403, body_key="error_csrf_body")
