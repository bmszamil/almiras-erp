"""Views of the web UI (docs/specs/web.md "As built: slice 1" and "As built: slice 2").

Thin by rule (R1 to R4): each view parses input, calls the owning module's service with ``request.user`` and renders
the result. Rights are checked by those services (``require`` / ``scope``); ``core.access.services.has`` is used only
to decide which dashboard cards and menu items to show (R6: hints only). Every page is under ``/e/<entity code>/``
and the entity is resolved only among the entities the user may see (R4, R10); anything else is 404.
Slice 1 is read-only. Slice 2 adds MFA enrolment and verification (``web.mfa``, R13 to R16) and the approval inbox,
whose only business POSTs are approve and send back, each one call of ``core.access.inbox.decide`` (R5, R61).
"""
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect

from core.access import inbox as inbox_services
from core.access import services as access_services
from core.access.inbox import InboxRefused
from core.access.services import AccessDenied
from core.models import label_part
from invoicing import services as invoicing_services
from invoicing.models import SalesDocument
from ledger import services as ledger_services

from . import mfa, services
from .forms import (ApproveForm, JournalEntryFilterForm, LoginForm, OtpCodeForm, RecoveryCodeForm,
                    SalesDocumentFilterForm, SendBackForm)
from .middleware import start_session
from .mfa import MfaError
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


# --- sign-in, sign-out, language (R12, R17, R18, R26, R27) -------------------------------------------------------

@never_cache
@csrf_protect
@_methods("GET", "HEAD", "POST")
def login_view(request):
    lang = request.web_lang
    next_url = _safe_next(request, request.POST.get("next") or request.GET.get("next"))
    if request.user.is_authenticated and request.method == "GET":
        return HttpResponseRedirect(next_url or reverse("web:home"))
    form = LoginForm(request.POST or None, lang=lang)
    error = None
    if request.method == "POST" and form.is_valid():
        result = services.sign_in(request, form.cleaned_data["username"], form.cleaned_data["password"])
        if result.locked:
            return _error(request, 429, body_key="error_429_user_body" if result.locked == "user" else None)
        if result.user is not None:
            login(request, result.user)  # rotates the session key (R17)
            start_session(request)
            services.log_login(result.user)
            return HttpResponseRedirect(next_url or reverse("web:home"))
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
        "inbox": True,  # every person may have approvals here (as approver or delegate); the inbox filters (R39)
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
        doc.customer_display = invoicing_services.listed_customer_name(doc, request.web_lang)
        doc.totals_ready = doc.status in invoicing_services.PRICED_STATUSES
    return _page(request, "web/sales_documents.html", entity, {**listing, "active": "sales"})


@require_GET
@entity_page
def sales_document(request, entity, pk):
    detail = invoicing_services.sales_document_detail(entity, pk, user=request.user)
    return _page(request, "web/sales_document.html", entity, {**detail, "active": "sales"})


# --- MFA: enrolment, verification, recovery codes (R13 to R16, R18, R26) ----------------------------------------

_MFA_STATUS = {"throttled": 429, "locked": 429}


def _mfa_error(error, lang):
    return text(f"mfa_err_{error.code}", lang)


def _next_of(request):
    return _safe_next(request, request.POST.get("next") or request.GET.get("next")) or ""


def _with_next(name, next_url):
    url = reverse(name)
    return f"{url}?{urlencode({'next': next_url})}" if next_url else url


# Views that check an MFA code run outside the request transaction (ATOMIC_REQUESTS): a later error (404, 403, 500)
# must not roll back the failure count, the lock, the used time step or the mfa_verify_failed row (security review
# 2026-10-21, item 1). Their writes are in the services' own transactions; business decisions in inbox.decide's.

@never_cache
@login_required
@_methods("GET", "HEAD", "POST")
def mfa_enrol(request):
    """Enrolment (R16). GET shows the start button, or the QR code and the confirm form of a started enrolment;
    POST starts (or restarts) it with a new secret, then shows it (post/redirect/get)."""
    user, lang = request.user, request.web_lang
    next_url = _next_of(request)
    if mfa.device_of(user) is not None:
        return HttpResponseRedirect(_with_next("web:mfa_verify", next_url))
    if not mfa.may_enrol(user):  # only people who need MFA, and staff (R13, R19)
        return redirect("web:mfa_security")
    if request.method == "POST":
        mfa.start_enrolment(user)
        return HttpResponseRedirect(_with_next("web:mfa_enrol", next_url))
    return _enrol_page(request, OtpCodeForm(lang=lang), None)


def _enrol_page(request, form, error, status=200):
    user = request.user
    device = mfa.pending_device(user)
    return render(request, "web/mfa_enrol.html", {
        "pending": device is not None, "setup": mfa.provisioning(user, device) if device is not None else None,
        "form": form, "error": error, "required": mfa.requires_mfa(user), "next": _next_of(request),
    }, status=status)


@transaction.non_atomic_requests
@never_cache
@require_POST
@login_required
def mfa_enrol_confirm(request):
    """A right code confirms the device: the session counts as verified (key rotated, R17) and the 10 recovery
    codes are shown on this response only, never stored in clear (R16)."""
    user, lang = request.user, request.web_lang
    form = OtpCodeForm(request.POST, lang=lang)
    if not form.is_valid():
        return _enrol_page(request, form, None, status=400)
    try:
        device, codes = mfa.confirm_enrolment(user, form.cleaned_data["code"])
    except MfaError as error:
        if error.code == "no_device":
            return redirect("web:mfa_enrol")
        return _enrol_page(request, OtpCodeForm(lang=lang), _mfa_error(error, lang),
                           status=_MFA_STATUS.get(error.code, 400))
    mfa.mark_verified(request.session, device)
    return render(request, "web/mfa_recovery_codes.html", {"codes": codes, "next": _next_of(request) or
                                                           reverse("web:home"), "first": True})


def _verify_page(request, error=None, status=200, form=None, recovery_form=None):
    lang = request.web_lang
    return render(request, "web/mfa_verify.html", {
        "form": form or OtpCodeForm(lang=lang), "recovery_form": recovery_form or RecoveryCodeForm(lang=lang),
        "error": error, "next": _next_of(request),
    }, status=status)


@transaction.non_atomic_requests
@never_cache
@csrf_protect
@login_required
@_methods("GET", "HEAD", "POST")
def mfa_verify(request):
    """Verification of the session with a TOTP code or a recovery code (R14), under the throttle (R18)."""
    user, lang = request.user, request.web_lang
    if mfa.device_of(user) is None:
        return redirect("web:mfa_enrol")
    next_url = _next_of(request) or reverse("web:home")
    if request.method != "POST":
        if mfa.session_verified(user, request.session):
            return HttpResponseRedirect(next_url)
        return _verify_page(request)
    with_recovery = "recovery_code" in request.POST
    form = (RecoveryCodeForm if with_recovery else OtpCodeForm)(request.POST, lang=lang)
    if not form.is_valid():
        return _verify_page(request, status=400, **({"recovery_form": form} if with_recovery else {"form": form}))
    try:
        if with_recovery:
            device = mfa.use_recovery_code(user, form.cleaned_data["recovery_code"])
        else:
            device = mfa.verify(user, form.cleaned_data["code"], purpose="sign_in")
    except MfaError as error:
        return _verify_page(request, _mfa_error(error, lang), status=_MFA_STATUS.get(error.code, 400))
    mfa.mark_verified(request.session, device)
    if with_recovery:
        messages.warning(request, text("mfa_recovery_used_notice", lang, count=mfa.recovery_codes_left(user)))
    return HttpResponseRedirect(next_url)


@transaction.non_atomic_requests
@never_cache
@login_required
@_methods("GET", "HEAD", "POST")
def mfa_security(request):
    """The person's own two-step verification page: device status, recovery codes left, renewal (R16)."""
    user, lang = request.user, request.web_lang
    step = mfa.gate(user, request.session)
    if step:
        return redirect("web:mfa_enrol" if step == "enrol" else "web:mfa_verify")
    device = mfa.device_of(user)
    if device is None:
        if mfa.may_enrol(user):
            return redirect("web:mfa_enrol")
        return render(request, "web/mfa_security.html", {"device": None, "entities": services.switcher_entities(user)})
    form = OtpCodeForm(request.POST or None, lang=lang)
    error, status = None, 200
    if request.method == "POST":
        if form.is_valid():
            try:
                codes = mfa.renew_recovery_codes(user, form.cleaned_data["code"])
            except MfaError as exc:
                error, status = _mfa_error(exc, lang), _MFA_STATUS.get(exc.code, 400)
            else:
                return render(request, "web/mfa_recovery_codes.html",
                              {"codes": codes, "next": reverse("web:mfa_security"), "first": False})
        else:
            status = 400
    return render(request, "web/mfa_security.html", {
        "device": device, "left": mfa.recovery_codes_left(user), "form": form, "error": error,
        "entities": services.switcher_entities(user),
    }, status=status)


# --- approval inbox (R6 to R8, R15, R39, R40; access §10 R61) ------------------------------------------------

def _step_up_needed(user):
    """R15: approving needs a fresh code from everyone who must use MFA (and anyone who enrolled)."""
    return mfa.must_verify(user)


@require_GET
@entity_page
def inbox(request, entity):
    """Open approval requests of this entity that the user may decide now (own membership or delegation)."""
    items = inbox_services.pending(request.user, entity)
    paginator = Paginator(items, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))
    for item in page.object_list:
        item.source_key = f"source_kind.{item.source_kind}"
    return _page(request, "web/inbox.html", entity, {"page": page, "paginator": paginator, "count": len(items),
                                                     "active": "inbox", "form": None})


def _item_page(request, entity, pk, *, approve_form=None, send_back_form=None, error=None, status=200):
    user, lang = request.user, request.web_lang
    found = inbox_services.item(user, entity, pk)
    found.why_key = f"inbox_why.{found.eligibility.why or 'not_eligible'}"
    step_up = _step_up_needed(user)
    version = {"version": found.version}
    context = {
        "item": found, "req": found.request, "step_up": step_up, "error": error, "active": "inbox",
        "decision_labels": inbox_services.decision_labels(),
        "approve_form": approve_form or ApproveForm(initial=version, lang=lang),
        "send_back_form": send_back_form or SendBackForm(initial=version, lang=lang),
    }
    response = _page(request, "web/inbox_item.html", entity, context)
    response.status_code = status
    return response


@require_GET
@entity_page
def inbox_item(request, entity, pk):
    return _item_page(request, entity, pk)


def _refusal_text(exc, lang):
    """The message of a refusal in the active language: the inbox's, the access layer's or a module's "en / ar"."""
    if isinstance(exc, InboxRefused):
        return exc.message_ar if lang == "ar" else exc.message_en
    return "; ".join(label_part(str(m), lang) for m in exc.messages)


def _decide(request, entity, pk, *, approve, form):
    """One engine decision (R5: one service call per action). Refusals change nothing and are shown: the access
    layer's 403 page (logged by the engine), or the inbox / module message on the item page."""
    user, lang = request.user, request.web_lang
    try:
        if approve:
            outcome = inbox_services.decide(user, entity, pk, version=form.cleaned_data["version"], approve=True)
        else:
            outcome = inbox_services.decide(user, entity, pk, version=form.cleaned_data["version"], approve=False,
                                            reason_ar=form.cleaned_data["reason_ar"],
                                            reason_en=form.cleaned_data["reason_en"])
    except AccessDenied as exc:
        return _error(request, 403, detail=(exc.message_en, exc.message_ar))
    except (InboxRefused, ValidationError) as exc:
        status = 409 if isinstance(exc, InboxRefused) and exc.code_name == "document_changed" else 400
        return _item_page(request, entity, pk, error=_refusal_text(exc, lang), status=status)
    if not approve:
        key = "inbox_done_returned" if outcome.decision == "return" else "inbox_done_rejected"
    else:
        key = "inbox_done_approved" if outcome.final else "inbox_done_step"
    messages.success(request, text(key, lang))
    return redirect("web:inbox", entity_code=entity.code)


def _session_check(request):
    """R15: an approval or send-back POST is refused unless the session is OTP-verified when the user needs MFA
    (checked here at the request, also behind the middleware gate)."""
    if mfa.must_verify(request.user) and not mfa.session_verified(request.user, request.session):
        return _error(request, 403, body_key="error_mfa_required_body")
    return None


@transaction.non_atomic_requests
@require_POST
@entity_page
def inbox_approve(request, entity, pk):
    """Approve: the version shown (R7) and, for MFA users, a fresh authenticator code (step-up, R15)."""
    refused_response = _session_check(request)
    if refused_response is not None:
        return refused_response
    user, lang = request.user, request.web_lang
    inbox_services.item(user, entity, pk)  # 404 before any code is checked: nothing to guess against (review item 1)
    step_up = _step_up_needed(user)
    form = ApproveForm(request.POST, lang=lang)
    if not step_up:
        form.fields["code"].required = False
    if not form.is_valid():
        return _item_page(request, entity, pk, approve_form=form, status=400)
    if step_up:
        try:
            mfa.verify(user, form.cleaned_data["code"], purpose="step_up")
        except MfaError as error:
            fresh = ApproveForm(initial={"version": form.cleaned_data["version"]}, lang=lang)
            return _item_page(request, entity, pk, approve_form=fresh, error=_mfa_error(error, lang),
                              status=_MFA_STATUS.get(error.code, 400))
    return _decide(request, entity, pk, approve=True, form=form)


@transaction.non_atomic_requests
@require_POST
@entity_page
def inbox_send_back(request, entity, pk):
    """Return or reject (as the document's module allows) with the reason: Arabic required, English optional (R8)."""
    refused_response = _session_check(request)
    if refused_response is not None:
        return refused_response
    inbox_services.item(request.user, entity, pk)  # 404 first, as for approve
    form = SendBackForm(request.POST, lang=request.web_lang)
    if not form.is_valid():
        return _item_page(request, entity, pk, send_back_form=form, status=400)
    return _decide(request, entity, pk, approve=False, form=form)


def refused(request):
    """The bilingual 403 page (used by the admin gate, R19)."""
    return _error(request, 403)


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
