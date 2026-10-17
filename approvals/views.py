"""REST API /api/v1/ (spec §6.1). Thin: validation in serializers, rules in services.

Every queryset is filtered by entity access (R1). Unknown or invisible ids return 404 (T1).
"""
import hashlib
import json
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.pagination import CursorPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from access import services as access
from access.errors import AccessDenied, BilingualError, Conflict, NotFound, RuleError
from access.models import AuditLog
from access.services import Context
from core.models import Entity

from . import notify, services
from .models import ApprovableType, ApprovalPolicy, ApprovalRequest, Delegation, IdempotencyRecord, Notification, PushDevice
from .serializers import (
    CancelSerializer, DecisionSerializer, DelegationSerializer, NotificationSerializer, PolicySerializer,
    RequestDetailSerializer, RequestListSerializer, SubmitSerializer, bi,
)


def ctx_for(request):
    channel = AuditLog.Channel.AGENT_GATEWAY if not request.user.is_human else AuditLog.Channel.API
    if request.headers.get("X-Client") == "mobile":
        channel = AuditLog.Channel.MOBILE
    elif request.headers.get("X-Client") == "web":
        channel = AuditLog.Channel.WEB
    return Context.from_request(request, channel=channel)


def _entity_param(request):
    code_or_id = request.query_params.get("entity")
    if not code_or_id:
        return None
    qs = Entity.objects.filter(code=code_or_id) if not code_or_id.isdigit() else Entity.objects.filter(pk=code_or_id)
    entity = qs.first()
    if entity is None or not access.has_perm(request.user, entity, "core.entity.view"):
        raise NotFound("entity_not_found", "Entity not found.", "المنشأة غير موجودة.")
    return entity


def _decimal_param(request, name):
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise RuleError("invalid_amount", f"{name} must be a number.", f"{name} يجب أن يكون رقماً.")


class Pagination(CursorPagination):
    page_size = 50
    ordering = ("-submitted_at", "-id")


# ---------------------------------------------------------------- /me


class MeView(APIView):
    def get(self, request):
        user = request.user
        entities = []
        for e in access.entities_with_perm(user, "core.entity.view"):
            entities.append({
                "id": e.pk, "code": e.code, "name": bi(e.name_en, e.name_ar), "is_group_parent": e.parent_id is None,
                "roles": sorted(access.active_grants(user, e).values_list("role__code", flat=True)),
                "permissions": sorted(access.permissions_for(user, e)),
            })
        return Response({
            "id": user.pk, "username": user.username, "name": bi(user.full_name_en, user.full_name_ar),
            "email": user.email, "language": user.language, "user_type": user.user_type,
            "default_entity": user.default_entity_id, "entities": entities,
        })

    def patch(self, request):
        language = request.data.get("language")
        default_entity = None
        if request.data.get("default_entity") is not None:
            default_entity = Entity.objects.filter(pk=request.data["default_entity"]).first()
            if default_entity is None:
                raise NotFound("entity_not_found", "Entity not found.", "المنشأة غير موجودة.")
        access.update_profile(request.user, language=language, default_entity=default_entity, ctx=ctx_for(request))
        return self.get(request)


# ---------------------------------------------------------------- requests


class RequestListView(APIView):
    def get(self, request):
        user = request.user
        scope = request.query_params.get("scope", "all")
        qs = services.visible_requests(user)
        entity = _entity_param(request)
        if entity is not None:
            qs = qs.filter(entity=entity)
        if st := request.query_params.get("status"):
            qs = qs.filter(status=st)
        if t := request.query_params.get("type"):
            qs = qs.filter(document_type__code=t)
        if (lo := _decimal_param(request, "amount_min")) is not None:
            qs = qs.filter(amount__gte=lo)
        if (hi := _decimal_param(request, "amount_max")) is not None:
            qs = qs.filter(amount__lte=hi)
        if scope == "mine":
            ids = [r.pk for r in qs.filter(status=ApprovalRequest.Status.PENDING) if services.eligibility(r, user)]
            qs = qs.filter(pk__in=ids)
        elif scope == "submitted":
            qs = qs.filter(submitted_by=user)
        elif scope == "delegated":
            qs = qs.filter(actions__on_behalf_of__isnull=False, actions__actor=user).distinct()
        elif scope != "all":
            raise RuleError("invalid_scope", "scope must be mine, submitted, delegated or all.",
                            "يجب أن يكون النطاق mine أو submitted أو delegated أو all.")
        paginator = Pagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(RequestListSerializer(page, many=True).data)

    def post(self, request):
        s = SubmitSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        r = services.submit_by_code(s.validated_data["document_type"], s.validated_data["object_id"], request.user,
                                    ctx=ctx_for(request))
        return Response(RequestDetailSerializer(r, context={"request": request}).data, status=status.HTTP_201_CREATED)


class RequestDetailView(APIView):
    def get(self, request, pk):
        r = services.get_visible(request.user, pk)
        return Response(RequestDetailSerializer(r, context={"request": request}).data)


class _IdempotentAction(APIView):
    """T22: the Idempotency-Key header is required; a repeat returns the stored response."""

    action = None

    def post(self, request, pk):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key or len(key) > 100:
            raise RuleError("idempotency_key_required", "The Idempotency-Key header is required.",
                            "ترويسة Idempotency-Key مطلوبة.")
        endpoint = f"{self.action}:{pk}"
        fingerprint = hashlib.sha256(json.dumps(request.data, sort_keys=True, default=str).encode()).hexdigest()
        stored = IdempotencyRecord.objects.filter(user=request.user, key=key).first()
        if stored:
            if stored.endpoint != endpoint or stored.fingerprint != fingerprint:
                raise RuleError("idempotency_key_reused", "This Idempotency-Key was used for a different call.",
                                "تم استخدام مفتاح عدم التكرار لطلب مختلف.")
            return Response(stored.body, status=stored.status_code)
        r = services.get_visible(request.user, pk)
        try:
            body, code = self.perform(request, r), 200
        except BilingualError as exc:
            body, code = exc.as_dict(), exc.status
        try:
            with transaction.atomic():
                IdempotencyRecord.objects.create(user=request.user, key=key, endpoint=endpoint,
                                                 fingerprint=fingerprint, status_code=code, body=body)
        except IntegrityError:
            stored = IdempotencyRecord.objects.get(user=request.user, key=key)
            return Response(stored.body, status=stored.status_code)
        return Response(body, status=code)

    def perform(self, request, r):
        raise NotImplementedError


class DecisionView(_IdempotentAction):
    def perform(self, request, r):
        s = DecisionSerializer(data=request.data)
        if not s.is_valid():
            raise RuleError("invalid_input", "Invalid input.", "مدخلات غير صالحة.", detail=s.errors)
        d = s.validated_data
        r = services.act(r, request.user, self.action, comment=d["comment"], document_version=d["document_version"],
                         on_behalf_of=d.get("on_behalf_of"), ctx=ctx_for(request))
        return RequestDetailSerializer(r, context={"request": request}).data


class ApproveView(DecisionView):
    action = "approve"


class RejectView(DecisionView):
    action = "reject"


class ReturnView(DecisionView):
    action = "return"


class CancelView(_IdempotentAction):
    action = "cancel"

    def perform(self, request, r):
        s = CancelSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        r = services.cancel(r, request.user, comment=s.validated_data["comment"], ctx=ctx_for(request))
        return RequestDetailSerializer(r, context={"request": request}).data


class SummaryView(APIView):
    def get(self, request):
        return Response(services.summary(request.user, _entity_param(request)))


# ---------------------------------------------------------------- delegations


class DelegationListView(APIView):
    def get(self, request):
        return Response(DelegationSerializer(services.visible_delegations(request.user), many=True).data)

    def post(self, request):
        s = DelegationSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        delegation = services.create_delegation(
            request.user, delegator=d.get("delegator") or request.user, delegate=d["delegate"],
            starts_at=d["starts_at"], ends_at=d["ends_at"], entity=d.get("entity"),
            document_types=d.get("document_types", []), amount_max=d.get("amount_max"),
            currency=d.get("currency", "SAR"), reason=d.get("reason", ""), ctx=ctx_for(request),
        )
        return Response(DelegationSerializer(delegation).data, status=status.HTTP_201_CREATED)


class DelegationDetailView(APIView):
    def patch(self, request, pk):
        delegation = services.visible_delegations(request.user).filter(pk=pk).first()
        if delegation is None:
            raise NotFound("not_found", "Delegation not found.", "التفويض غير موجود.")
        if request.data.get("end") is not True:
            raise RuleError("only_end", "Delegations can only be ended ({\"end\": true}).",
                            "يمكن إنهاء التفويضات فقط.")
        delegation = services.end_delegation(request.user, delegation, ctx=ctx_for(request))
        return Response(DelegationSerializer(delegation).data)


# ---------------------------------------------------------------- policies (R7)


class PolicyListView(APIView):
    def get(self, request):
        return Response(PolicySerializer(services.manageable_policies(request.user), many=True).data)

    def post(self, request):
        s = PolicySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = dict(s.validated_data)
        steps = [dict(x) for x in data.pop("steps")]
        p = services.create_policy(request.user, steps=steps, ctx=ctx_for(request), **data)
        return Response(PolicySerializer(p).data, status=status.HTTP_201_CREATED)


class PolicyDetailView(APIView):
    def patch(self, request, pk):
        policy = services.manageable_policies(request.user).filter(pk=pk).first()
        if policy is None:
            raise NotFound("not_found", "Policy not found.", "السياسة غير موجودة.")
        s = PolicySerializer(policy, data=request.data, partial=True)
        s.is_valid(raise_exception=True)
        data = dict(s.validated_data)
        if "steps" in data:
            raise RuleError("steps_immutable", "Steps cannot be edited here; create a new policy version.",
                            "لا يمكن تعديل الخطوات هنا؛ أنشئ نسخة جديدة من السياسة.")
        p = services.update_policy(request.user, policy, ctx=ctx_for(request), **data)
        return Response(PolicySerializer(p).data)


# ---------------------------------------------------------------- notifications, devices, audit


class NotificationListView(APIView):
    def get(self, request):
        qs = Notification.objects.filter(user=request.user)
        if request.query_params.get("unread") in {"1", "true"}:
            qs = qs.filter(read_at__isnull=True)
        return Response(NotificationSerializer(qs[:200], many=True).data)


class NotificationReadView(APIView):
    def post(self, request, pk):
        n = get_object_or_404(Notification, pk=pk, user=request.user)
        notify.mark_read(n, request.user)
        return Response(NotificationSerializer(n).data)


class DeviceView(APIView):
    """Push hook for the later mobile wave: accepted and stored, not used to send (D4)."""

    def post(self, request):
        platform, token = request.data.get("platform"), request.data.get("token")
        if platform not in PushDevice.Platform.values or not token:
            raise RuleError("invalid_device", "platform (ios/android) and token are required.",
                            "المنصة (ios/android) والرمز مطلوبان.")
        with transaction.atomic():
            existing = PushDevice.objects.select_for_update().filter(token=token).first()
            if existing is not None and existing.user_id != request.user.pk:
                # Review item 10: a token registered by another user is never taken over.
                raise Conflict("device_token_taken", "This device token is registered to another user.",
                               "رمز هذا الجهاز مسجل لمستخدم آخر.")
            if existing is None:
                device = PushDevice.objects.create(user=request.user, platform=platform, token=token)
            else:
                existing.platform, existing.is_active, existing.last_seen_at = platform, True, timezone.now()
                existing.save(update_fields=["platform", "is_active", "last_seen_at"])
                device = existing
        access.audit(request.user, "device.register", device, after={"platform": platform}, ctx=ctx_for(request))
        return Response({"id": device.pk, "platform": device.platform}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        devices = PushDevice.objects.filter(user=request.user, token=request.data.get("token", ""), is_active=True)
        for device in devices:
            device.is_active = False
            device.save(update_fields=["is_active"])
            access.audit(request.user, "device.unregister", device, ctx=ctx_for(request))
        return Response(status=status.HTTP_204_NO_CONTENT)


class AuditView(APIView):
    def get(self, request):
        qs = access.visible_audit(request.user)
        if not access.entities_with_perm(request.user, "access.audit.view").exists():
            raise AccessDenied("permission_denied", "You cannot read the audit log.", "لا يمكنك الاطلاع على سجل التدقيق.")
        entity = _entity_param(request)
        if entity is not None:
            qs = qs.filter(entity=entity)
        if ot := request.query_params.get("object_type"):
            qs = qs.filter(object_type=ot)
        if oid := request.query_params.get("object_id"):
            qs = qs.filter(object_id=oid)
        security_entities = set(access.entities_with_perm(request.user, "access.audit.view_security").values_list("pk", flat=True))
        security_global = access.entities_with_perm(request.user, "access.audit.view_security").filter(parent__isnull=True).exists()
        rows = []
        for a in qs.select_related("actor", "on_behalf_of", "entity")[:500]:
            row = {
                "id": a.pk, "at": a.at, "actor": a.actor.username,
                "on_behalf_of": a.on_behalf_of.username if a.on_behalf_of_id else None,
                "entity": a.entity.code if a.entity_id else None, "action": a.action,
                "object_type": a.object_type, "object_id": a.object_id, "before": a.before, "after": a.after,
                "source": a.source, "channel": a.channel, "request_id": str(a.request_id),
            }
            # IP address and device only with access.audit.view_security (compliance).
            if (a.entity_id in security_entities) if a.entity_id else security_global:
                row["ip"], row["user_agent"] = a.ip, a.user_agent
            rows.append(row)
        return Response(rows)


class AccessReviewView(APIView):
    """Quarterly access review (compliance): user x entity x role, last login, 2FA."""

    def get(self, request):
        if not (access.entities_with_perm(request.user, "access.audit.view").exists()
                or access.entities_with_perm(request.user, "access.entity_access.manage").exists()):
            raise AccessDenied("permission_denied", "You cannot read the access review.", "لا يمكنك الاطلاع على مراجعة الصلاحيات.")
        return Response(access.access_review_report(request.user, ctx=ctx_for(request)))


class SelfApprovalReportView(APIView):
    """D10: monthly self-approval report for the Chairman and auditor."""

    def get(self, request):
        try:
            year, month = int(request.query_params["year"]), int(request.query_params["month"])
        except (KeyError, ValueError):
            raise RuleError("invalid_period", "year and month are required.", "السنة والشهر مطلوبان.")
        if not access.entities_with_perm(request.user, "access.audit.view").exists():
            raise AccessDenied("permission_denied", "You cannot read this report.", "لا يمكنك الاطلاع على هذا التقرير.")
        return Response(services.self_approval_report(request.user, year, month))


__all__ = ["ApprovableType", "ApprovalPolicy", "Delegation"]
