"""The monthly bilingual governance / internal-audit report (access.md §10; R59, R84 to R86; OQ-33, OQ-51, OQ-54).

On the 1st of each month at 02:00 Riyadh time the ``system.governance`` user generates one report per active entity
for the previous month (:func:`run_monthly_reports`). Each report has the same sections in English and Arabic:

1. self-approvals by the top authority (R83);
2. delegations granted, revoked and expired, and every decision made under a delegation (``on_behalf_of``);
3. authority matrix versions drafted, submitted, approved, rejected, withdrawn and taking effect (with the rows),
   and changes of the top authority and the self-approval switch;
4. approvals decided under code defaults (``rule_source = default``, R70);
5. refused self-approval attempts;
6. personal-assistant assignments;
7. for the group parent only: group-level actions (entity settings, Sharia switch-off requests and decisions,
   global exchange-rate changes).

A month with no events says "Nothing to report / لا يوجد ما يُبلَّغ عنه". A generated report never changes
(``content_hash``, R85); a correction is a new revision naming the one it replaces. It is read only by holders of
``core.view_governance_report`` **in that entity** whose role is a report recipient (default: the internal auditor
and the top authority), an external auditor while their membership lasts, or a board representative (OQ-33, OQ-42);
every view and export is logged; an internal auditor records the review once (R86). Unreviewed after 30 days: an
alert to the top authority. In-app only; PDF / CSV exports for the audit committee or board on request (OQ-54).
"""
import csv
import io
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.http import Http404

from core.models import Entity

from . import catalogue, engine
from . import services as access
from .authority_models import (
    AccessAlert, ApprovalDecision, ApprovalDelegation, AssistantAssignment, GovernanceReport, canonical_hash,
)
from .models import AccessLog, GrantUse

RIYADH = ZoneInfo("Asia/Riyadh")
VIEW = "core.view_governance_report"
REVIEW = "core.review_governance_report"
GENERATE = "core.generate_governance_report"
REVIEW_DAYS = 30
RUN_HOUR = 2
EXTRA_READERS = frozenset({"external_auditor", "board_representative"})

SECTIONS = (
    ("self_approvals", "Self-approvals by the top authority", "الاعتمادات الذاتية من السلطة العليا"),
    ("delegations", "Delegations and decisions under delegation", "التفويضات والقرارات المتخذة بموجبها"),
    ("authority_matrix", "Authority matrix and top-authority changes", "تغييرات مصفوفة الصلاحيات والسلطة العليا"),
    ("default_rule_approvals", "Approvals under code defaults (no matrix row)",
     "الاعتمادات وفق الإعدادات الافتراضية (لا يوجد سطر في المصفوفة)"),
    ("refused_self_approvals", "Refused self-approval attempts", "محاولات الاعتماد الذاتي المرفوضة"),
    ("assistant_assignments", "Personal assistant assignments", "تعيينات المساعدين الشخصيين"),
    ("group_actions", "Group-level actions (group parent only)", "الإجراءات على مستوى المجموعة (الكيان الأم فقط)"),
)
NOTHING = ("Nothing to report", "لا يوجد ما يُبلَّغ عنه")
TITLE = ("Monthly governance report", "تقرير الحوكمة الشهري")
G_PERMISSIONS = frozenset(c for c, p in catalogue.CATALOGUE.items() if p.is_group_only)


def month_start(value):
    return date(value.year, value.month, 1)


def _window(month):
    start = datetime.combine(month, time.min, tzinfo=RIYADH)
    nxt = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return start, datetime.combine(nxt, time.min, tzinfo=RIYADH)


def previous_month(at):
    local = at.astimezone(RIYADH).date()
    first = date(local.year, local.month, 1)
    return month_start(first - timedelta(days=1))


def _who(user):
    if user is None:
        return None
    return {"id": user.pk, "username": user.get_username()}


def _iso(value):
    return value.astimezone(RIYADH).isoformat() if value else None


def _logs(entity, start, end, *events, **filters):
    return AccessLog.objects.filter(entity=entity, at__gte=start, at__lt=end, event__in=events, **filters) \
        .select_related("actor").order_by("at", "id")


def _log_item(row):
    return {"at": _iso(row.at), "event": row.event, "user": _who(row.actor), "permission": row.permission,
            "details": row.details}


def _counts(decision):
    """A decision counts once its grant was used, for a permission that posts; others count when granted."""
    log = decision.grant_log
    if log is None:
        return True
    perm = catalogue.CATALOGUE.get(log.permission)
    return not (perm and perm.posts) or GrantUse.objects.filter(log_id=log.pk).exists()


def build_content(entity, month, *, revision=1, replaces=None, reason=""):
    """The report's content for ``entity`` and ``month`` (R84), as plain bilingual data."""
    start, end = _window(month)
    s = {}
    s["self_approvals"] = [_log_item(r) for r in _logs(entity, start, end, AccessLog.Event.SELF_APPROVAL)]
    delegations = ApprovalDelegation.objects.filter(entity=entity).select_related("delegator", "delegate")

    def _d(d):
        return {"delegation": d.pk, "delegator": _who(d.delegator), "delegate": _who(d.delegate),
                "valid_from": _iso(d.valid_from), "valid_to": _iso(d.valid_to), "amount_max":
                    None if d.amount_max is None else str(d.amount_max),
                "activities": sorted(d.activities.values_list("code", flat=True)), "reason_en": d.reason_en,
                "reason_ar": d.reason_ar}

    decisions = (ApprovalDecision.objects.filter(entity=entity, at__gte=start, at__lt=end, delegation__isnull=False,
                                                 decision=ApprovalDecision.Decision.APPROVED)
                 .select_related("user", "on_behalf_of", "request", "request__activity", "grant_log"))
    s["delegations"] = {
        "granted": [_d(d) for d in delegations.filter(created_at__gte=start, created_at__lt=end)],
        "revoked": [{**_d(d), "revoked_at": _iso(d.revoked_at), "revoke_reason": d.revoke_reason}
                    for d in delegations.filter(revoked_at__gte=start, revoked_at__lt=end)],
        "expired": [_d(d) for d in delegations.filter(valid_to__gte=start, valid_to__lt=end)
                    .filter(Q(revoked_at__isnull=True) | Q(revoked_at__gt=end))],
        "decisions": [_decision(d) for d in decisions if _counts(d)],
    }
    s["authority_matrix"] = [_log_item(r) for r in _logs(
        entity, start, end, AccessLog.Event.MATRIX_DRAFTED, AccessLog.Event.MATRIX_ROW_ADDED,
        AccessLog.Event.MATRIX_ROW_REMOVED, AccessLog.Event.MATRIX_SUBMITTED, AccessLog.Event.MATRIX_APPROVED,
        AccessLog.Event.MATRIX_REJECTED, AccessLog.Event.MATRIX_WITHDRAWN, AccessLog.Event.MATRIX_EFFECTIVE,
        AccessLog.Event.GOVERNANCE_CHANGE_REQUESTED)] + [
        _log_item(r) for r in _logs(entity, start, end, AccessLog.Event.ENTITY_SETTINGS_CHANGED, details__governance=True)]
    defaults = (ApprovalDecision.objects.filter(entity=entity, at__gte=start, at__lt=end, rule_source="default",
                                                decision=ApprovalDecision.Decision.APPROVED)
                .select_related("user", "on_behalf_of", "request", "request__activity", "grant_log"))
    s["default_rule_approvals"] = [_decision(d) for d in defaults if _counts(d)]
    s["refused_self_approvals"] = [_log_item(r) for r in _logs(entity, start, end, AccessLog.Event.PERMISSION_DENIED,
                                                               details__reason="self_approval")]
    assignments = AssistantAssignment.objects.filter(entity=entity).select_related("assistant", "principal")
    s["assistant_assignments"] = [
        {"assignment": a.pk, "assistant": _who(a.assistant), "principal": _who(a.principal),
         "valid_from": _iso(a.valid_from), "valid_to": _iso(a.valid_to), "reason": a.reason,
         "revoked_at": _iso(a.revoked_at), "revoke_reason": a.revoke_reason}
        for a in assignments.filter(Q(created_at__gte=start, created_at__lt=end)
                                    | Q(revoked_at__gte=start, revoked_at__lt=end))]
    if engine.is_group_parent(entity):
        group_rows = AccessLog.objects.filter(at__gte=start, at__lt=end).filter(
            Q(event=AccessLog.Event.ENTITY_SETTINGS_CHANGED)
            | Q(event__in=(AccessLog.Event.DUAL_CONTROL_REQUESTED, AccessLog.Event.DUAL_CONTROL_DECIDED,
                           AccessLog.Event.DUAL_CONTROL_USED), permission="core.disable_sharia_enforcement")
            | Q(permission__in=G_PERMISSIONS) & ~Q(event=AccessLog.Event.PERMISSION_DENIED)
        ).filter(Q(entity=entity) | Q(entity__parent=entity) | Q(entity__isnull=True)).select_related("actor")
        s["group_actions"] = [_log_item(r) for r in group_rows.order_by("at", "id")]
    else:
        s["group_actions"] = None
    empty = not any(_has_items(v) for v in s.values())
    return {
        "entity": entity.code, "entity_name_en": entity.name_en, "entity_name_ar": entity.name_ar,
        "month": month.strftime("%Y-%m"), "revision": revision, "replaces": replaces, "reissue_reason": reason,
        "title": {"en": TITLE[0], "ar": TITLE[1]},
        "labels": {key: {"en": en, "ar": ar} for key, en, ar in SECTIONS},
        "sections": s, "nothing_to_report": empty, "nothing_to_report_text": {"en": NOTHING[0], "ar": NOTHING[1]},
    }


def _has_items(value):
    if value is None:
        return False
    if isinstance(value, dict):
        return any(_has_items(v) for v in value.values())
    return bool(value)


def _decision(d):
    req = d.request
    return {"at": _iso(d.at), "user": _who(d.user), "on_behalf_of": _who(d.on_behalf_of), "activity": req.activity.code,
            "document": [req.document_type, req.document_id], "label_en": req.label_en, "label_ar": req.label_ar,
            "amount": None if req.amount is None else str(req.amount), "currency": req.currency,
            "rule_source": d.rule_source, "rule_id": d.rule_id, "matrix_version": req.matrix_version_id,
            "self_approval": d.self_approval, "step_no": d.step_no,
            "grant_log": d.grant_log_id}


def _save(obj):
    obj._service_write = True
    obj.save()
    return obj


@transaction.atomic
def generate_report(entity, month, *, user, reissue_reason=""):
    """Generate ``entity``'s report for ``month`` as ``user`` (the ``system.governance`` user, R84). Without
    ``reissue_reason`` an existing report is returned unchanged; with it, a new revision names the earlier one
    (R85)."""
    entity = Entity.objects.get(pk=entity.pk)
    access.require(user, entity, GENERATE)
    month = month_start(month)
    latest = GovernanceReport.objects.filter(entity=entity, month=month).order_by("-revision").first()
    if latest is not None and not reissue_reason:
        return latest
    revision = latest.revision + 1 if latest else 1
    content = build_content(entity, month, revision=revision, replaces=latest.revision if latest else None,
                            reason=reissue_reason)
    report = _save(GovernanceReport(entity=entity, month=month, revision=revision, replaces=latest,
                                    generated_at=access.now(), generated_by=user, content=content,
                                    content_hash=canonical_hash(content)))
    access.log(AccessLog.Event.REPORT_GENERATED, actor=user, entity=entity, permission=GENERATE,
               details={"report": report.pk, "month": content["month"], "revision": revision,
                        "replaces": latest.pk if latest else None, "content_hash": report.content_hash,
                        "reason": reissue_reason})
    return report


def run_monthly_reports(at=None):
    """Job: from 02:00 Riyadh time on the 1st, the previous month's report for every active entity (R84). Returns
    (generated, skipped): entities where the governance system user has no membership are skipped and their top
    authority is told (setup data, like every system user)."""
    at = at or access.now()
    local = at.astimezone(RIYADH)
    if local.day == 1 and local.hour < RUN_HOUR:
        month = previous_month(at - timedelta(days=1))
    else:
        month = previous_month(at)
    system = access.system_user("governance")
    generated, skipped = [], []
    for entity in Entity.objects.filter(is_active=True).order_by("code"):
        if not access.has(system, entity, GENERATE):
            skipped.append(entity)
            for uid in engine.top_authority_holders(entity, at):
                engine.alert(entity, uid, AccessAlert.Kind.REPORT_MISSING_USER,
                             f"{entity.code}: the governance report for {month:%Y-%m} was not generated: the governance "
                             "system user has no membership in this entity (setup).",
                             f"{entity.code}: لم يُنشأ تقرير الحوكمة لشهر {month:%Y-%m}: لا توجد عضوية لمستخدم نظام "
                             "الحوكمة في هذا الكيان.", key=f"report_missing:{entity.pk}:{month:%Y-%m}:{uid}")
            continue
        generated.append(generate_report(entity, month, user=system))
    return generated, skipped


# --- reading, exports and review (R86) -------------------------------------------------------------------------------

def _reader_roles(user, entity):
    at = access.now()
    ms = [m for m in access._memberships_for(user, entity, VIEW, at) if m.entity_id == entity.pk]
    recipients = engine.governance_of(entity).recipients | EXTRA_READERS
    return {m.role.code for m in ms if m.role.code in recipients}


def _require_reader(user, report):
    access.require(user, report.entity, VIEW)
    roles = _reader_roles(user, report.entity)
    if not roles:
        access.deny(user, report.entity, VIEW, "no_membership")
    return roles


def view_report(report, *, user):
    """The report's content for a reader of that entity (R86); logged."""
    report = GovernanceReport.objects.select_related("entity").get(pk=report.pk)
    roles = _require_reader(user, report)
    access.log(AccessLog.Event.REPORT_VIEWED, actor=user, entity=report.entity, permission=VIEW,
               details={"report": report.pk, "roles": sorted(roles)})
    return report.content


def export_report(report, *, user, fmt="csv"):
    """CSV or PDF export (OQ-54); logged. Returns bytes."""
    report = GovernanceReport.objects.select_related("entity").get(pk=report.pk)
    roles = _require_reader(user, report)
    if fmt not in ("csv", "pdf"):
        raise ValidationError("Exports are CSV or PDF. / التصدير بصيغة CSV أو PDF.")
    data = _csv(report.content) if fmt == "csv" else _pdf(report.content)
    access.log(AccessLog.Event.REPORT_EXPORTED, actor=user, entity=report.entity, permission=VIEW,
               details={"report": report.pk, "format": fmt, "roles": sorted(roles), "bytes": len(data)})
    return data


@transaction.atomic
def review_report(report, *, user, note_en, note_ar):
    """The internal auditor records the review once, with a bilingual note (R86)."""
    report = GovernanceReport.objects.select_for_update().select_related("entity").get(pk=report.pk)
    access.require(user, report.entity, REVIEW)
    if not [m for m in access._memberships_for(user, report.entity, REVIEW, access.now())
            if m.entity_id == report.entity_id]:
        access.deny(user, report.entity, REVIEW, "no_membership")
    if report.reviewed_at is not None:
        raise ValidationError("This report has already been reviewed; the review is recorded once (R86). / "
                              "رُوجع هذا التقرير بالفعل؛ تُسجَّل المراجعة مرة واحدة.", code="already_reviewed")
    if not (note_en or "").strip() or not (note_ar or "").strip():
        raise ValidationError("A review note in English and Arabic is required. / ملاحظة المراجعة بالإنجليزية والعربية مطلوبة.")
    report.reviewed_by, report.reviewed_at = user, access.now()
    report.review_note_en, report.review_note_ar = note_en, note_ar
    _save(report)
    access.log(AccessLog.Event.REPORT_REVIEWED, actor=user, entity=report.entity, permission=REVIEW,
               details={"report": report.pk, "note_en": note_en, "note_ar": note_ar})
    return report


def send_unreviewed_alerts(at=None):
    """Job (daily): a report not reviewed within 30 days of its generation alerts the top authority (R86)."""
    at = at or access.now()
    sent = []
    for report in GovernanceReport.objects.filter(reviewed_at__isnull=True,
                                                  generated_at__lte=at - timedelta(days=REVIEW_DAYS)).select_related("entity"):
        if GovernanceReport.objects.filter(entity=report.entity, month=report.month, revision__gt=report.revision).exists():
            continue
        for uid in engine.top_authority_holders(report.entity, at):
            row = engine.alert(report.entity, uid, AccessAlert.Kind.REPORT_UNREVIEWED,
                               f"{report.entity.code}: the governance report for {report.month:%Y-%m} has not been "
                               f"reviewed {REVIEW_DAYS} days after it was generated (R86).",
                               f"{report.entity.code}: لم يُراجع تقرير الحوكمة لشهر {report.month:%Y-%m} بعد {REVIEW_DAYS} "
                               "يومًا من إنشائه.", key=f"report_unreviewed:{report.pk}:{uid}",
                               details={"report": report.pk})
            if row is not None:
                sent.append(row)
    return sent


def reports(user, entity=None):
    """Reports ``user`` may read: entities where they hold the right through a reader role of their own (R86, R90)."""
    ids = [e.pk for e in access.entities_for(user, VIEW) if _reader_roles(user, e)]
    qs = GovernanceReport.objects.filter(entity_id__in=ids)
    return qs.filter(entity=entity) if entity is not None else qs


def get_report(user, pk):
    try:
        return reports(user).get(pk=pk)
    except GovernanceReport.DoesNotExist:
        raise Http404("Not found / غير موجود") from None


# --- export formats --------------------------------------------------------------------------------------------------

def _rows(content):
    for key, en, ar in SECTIONS:
        value = content["sections"].get(key)
        if value is None:
            continue
        items = []
        if isinstance(value, dict):
            for sub, rows in value.items():
                items += [(sub, r) for r in rows]
        else:
            items = [("", r) for r in value]
        if not items:
            yield key, en, ar, "", {"en": NOTHING[0], "ar": NOTHING[1]}
        for sub, item in items:
            yield key, en, ar, sub, item


def _csv(content):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["section", "section_en", "section_ar", "part", "at", "user", "detail"])
    writer.writerow(["report", content["title"]["en"], content["title"]["ar"], content["month"], "", "",
                     f"{content['entity']} revision {content['revision']}"])
    if content["nothing_to_report"]:
        writer.writerow(["report", NOTHING[0], NOTHING[1], "", "", "", ""])
    for key, en, ar, sub, item in _rows(content):
        user = (item.get("user") or {}).get("username", "") if isinstance(item, dict) else ""
        writer.writerow([key, en, ar, sub, item.get("at", "") if isinstance(item, dict) else "", user,
                         access.json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)])
    return ("﻿" + out.getvalue()).encode("utf-8")


def _font():
    """A TrueType font with Arabic glyphs (setting GOVERNANCE_REPORT_FONT, else DejaVu Sans if installed)."""
    import os

    from django.conf import settings
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [getattr(settings, "GOVERNANCE_REPORT_FONT", None), "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                  "/usr/share/fonts/dejavu/DejaVuSans.ttf"]
    for path in candidates:
        if path and os.path.exists(path):
            if "GovReport" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("GovReport", path))
            return "GovReport"
    return "Helvetica"


def _pdf(content):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from .arabic_pdf import visual

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4, invariant=1)
    pdf.setTitle(f"{content['title']['en']} {content['entity']} {content['month']} r{content['revision']}")
    font = _font()
    width, height = A4
    y = height - 50

    def line(en, ar="", size=9):
        nonlocal y
        if y < 50:
            pdf.showPage()
            y = height - 50
        pdf.setFont(font, size)
        pdf.drawString(40, y, str(en)[:110])
        if ar:
            pdf.drawRightString(width - 40, y - size - 3, visual(str(ar))[:110])
            y -= size + 3
        y -= size + 6

    line(f"{content['title']['en']} - {content['entity_name_en']} - {content['month']} (revision {content['revision']})",
         f"{content['title']['ar']} - {content['entity_name_ar']} - {content['month']}", 12)
    if content["nothing_to_report"]:
        line(NOTHING[0], NOTHING[1], 11)
    for key, en, ar in SECTIONS:
        value = content["sections"].get(key)
        if value is None:
            continue
        line(en, ar, 11)
        rows = [(s, r) for s, rs in value.items() for r in rs] if isinstance(value, dict) else [("", r) for r in value]
        if not rows:
            line(f"  {NOTHING[0]}", NOTHING[1])
        for sub, item in rows:
            user = (item.get("user") or {}).get("username", "")
            line(f"  {sub + ': ' if sub else ''}{item.get('at', '')} {user} {item.get('event', item.get('activity', ''))}")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()

