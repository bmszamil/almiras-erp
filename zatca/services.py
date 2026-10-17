"""ZATCA Phase 2 e-invoicing services (docs/specs/zatca.md). The only supported way to change zatca data.

Every public function checks rights with ``core.access`` for the entity that owns the record (R-3, access R1).
This module creates **no journal entries** (§5): results go back to ``invoicing.services.on_zatca_result``,
which posts. Key material never touches the database: the secret store holds it (``zatca.keystore``; D-3,
D-12, R-6); ZATCA is reached only through ``zatca.client`` (R-29).

Flow for a sales document (R-20): invoicing issues it and calls the port -> :func:`submit_document` creates
the ``EInvoice`` (``pending``) -> the queue worker (:func:`process_queue`, ZATCA queue system user) stamps it
(ICV, PIH, hash, signature, QR; ``stamped`` -> ``queued``) and submits it (``submitting`` -> cleared /
reported / rejected / failed) -> the result is handed to invoicing.
"""
import base64
import hashlib
import json
import logging
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from lxml import etree

from core import access
from core.access import services as access_services
from core.access.models import AccessLog, Kind, Membership
from core.models import Entity

from . import client as zatca_client
from . import crypto, keystore, qr, rules, ubl
from .keystore import SecretStoreRefused, SecretStoreUnavailable
from .messages import ZatcaError, arabic_for, local
from .models import (
    ComplianceTask,
    EgsCredential,
    EgsUnit,
    EInvoice,
    EntityMasterKey,
    GroupSuperKey,
    KeyCustodianDelegation,
    OnboardingEvent,
    Source,
    SubmissionAttempt,
    ValidationMessage,
    ZatcaAlert,
    ZatcaProfile,
    check_no_secret,
)

log = logging.getLogger("zatca")
RIYADH = ZoneInfo("Asia/Riyadh")
Status = EInvoice.Status
EgsStatus = EgsUnit.Status
Action = OnboardingEvent.Action
Scope = KeyCustodianDelegation.Scope

CLEARED = frozenset({Status.CLEARED, Status.CLEARED_WITH_WARNINGS})
REPORTED = frozenset({Status.REPORTED, Status.REPORTED_WITH_WARNINGS})
FINAL_STATUSES = CLEARED | REPORTED | {Status.REPORTED_CLEARANCE_DISABLED, Status.REJECTED, Status.FAILED_FINAL}
# R-20: the only status changes.
TRANSITIONS = {
    Status.PENDING: {Status.STAMPED},
    Status.STAMPED: {Status.QUEUED},
    Status.QUEUED: {Status.SUBMITTING},
    Status.SUBMITTING: {Status.CLEARED, Status.CLEARED_WITH_WARNINGS, Status.REPORTED, Status.REPORTED_WITH_WARNINGS,
                        Status.REPORTED_CLEARANCE_DISABLED, Status.REJECTED, Status.FAILED_RETRYABLE,
                        Status.FAILED_FINAL},
    Status.FAILED_RETRYABLE: {Status.QUEUED},
}
# R-5: EGS status changes (any -> revoked handled separately).
EGS_TRANSITIONS = {
    EgsStatus.DRAFT: {EgsStatus.CSR_READY},
    EgsStatus.CSR_READY: {EgsStatus.COMPLIANCE_CSID},
    EgsStatus.COMPLIANCE_CSID: {EgsStatus.COMPLIANCE_PASSED, EgsStatus.DRAFT},
    EgsStatus.COMPLIANCE_PASSED: {EgsStatus.ACTIVE},
    EgsStatus.ACTIVE: {EgsStatus.RENEWAL_DUE, EgsStatus.EXPIRED, EgsStatus.ACTIVE},
    EgsStatus.RENEWAL_DUE: {EgsStatus.ACTIVE, EgsStatus.EXPIRED},
}
RETRIED_HOLDS = ("", EInvoice.Hold.NO_EGS, EInvoice.Hold.EGS_NOT_READY)
PROCESS = "zatca.process_queue"
SWITCH = "zatca.switch_to_production"


def _setting(name, default):
    return getattr(settings, name, default)


def _now():
    """Current time (a function so tests can move the clock)."""
    return timezone.now()


def _save(obj, **kwargs):
    obj._service_write = True
    obj.save(**kwargs)
    return obj


def _source(user):
    kind = access_services.kind_of(user)
    return {Kind.AGENT: Source.AGENT, Kind.SYSTEM: Source.SYSTEM}.get(kind, Source.HUMAN)


def _queue_user():
    return access.system_user("zatca_queue")


def _refuse(user, entity, permission, code, en, ar):
    """Log a ``permission_denied`` row (access R27) and raise AccessDenied with a zatca message."""
    access_services.log_denial(actor=user, entity=entity, permission=permission, details={"reason": code})
    raise access.AccessDenied(code, en, ar, access_services._language(user))


def _event(entity, egs, action, actor, **detail):
    check_no_secret(detail)
    return OnboardingEvent.objects.create(entity=entity, egs_unit=egs, action=action, actor=actor, detail=detail)


# --- alerts (R-10, R-23, R-44, R-47) ----------------------------------------------------------------

def _active_members(entity, role_code):
    from django.contrib.auth import get_user_model

    ids = Membership.objects.active(_now()).filter(entity=entity, role__code=role_code).values_list("user_id", flat=True)
    return list(get_user_model().objects.filter(pk__in=ids).order_by("pk"))


def _zatca_admins(entity):
    return _active_members(entity, "zatca_admin")


def _managing_partners(entity):
    return _active_members(access.group_parent_of(entity), "managing_partner")


def _master_key_custodians(entity):
    at = _now()
    return [d.custodian for d in KeyCustodianDelegation.objects.filter(entity=entity, scope=Scope.MASTER_KEY)
            .select_related("custodian") if d.is_active(at)]


def _alert(entity, recipients, kind, en, ar, *, einvoice=None, egs=None):
    seen = set()
    for user in recipients:
        if user.pk in seen:
            continue
        seen.add(user.pk)
        ZatcaAlert.objects.create(entity=entity, recipient=user, kind=kind, einvoice=einvoice, egs_unit=egs,
                                  message_en=en, message_ar=ar)


# --- key hierarchy and custody (D-12, R-41, R-50) --------------------------------------------------------

def _active_delegation(user, entity, scope):
    at = _now()
    return any(d.is_active(at) for d in KeyCustodianDelegation.objects.filter(entity=entity, custodian=user,
                                                                               scope=scope))


def _require_key_action(user, entity, scope):
    """R-41: ``zatca.onboard`` (zatca_admin, never agents) **and** an active delegation for the entity and scope."""
    grant = access.require(user, entity, "zatca.onboard")
    if not _active_delegation(user, entity, scope):
        _refuse(user, entity, "zatca.onboard", "no_key_delegation",
                f"You hold no active key custody delegation ({scope}) for {entity.code} (R-41).",
                f"ليس لديك تفويض نشط لحفظ المفاتيح ({scope}) للكيان {entity.code}.")
    return grant


def _master_ref(entity):
    mk = EntityMasterKey.objects.filter(entity=entity).first()
    if mk is None:
        raise ZatcaError(f"{entity.code} has no master key yet; the Managing Partner creates it first (R-50 b).",
                         f"ليس للكيان {entity.code} مفتاح رئيسي بعد؛ ينشئه الشريك المدير أولًا.", code="no_master_key")
    return keystore.make_ref(keystore.parse_ref(mk.key_ref)[0], mk.key_version)


def register_group_super_key(group_parent, *, user):
    """D-12 / R-50: the group's super key, held by its Managing Partner. The ERP keeps the reference only."""
    access.require(user, group_parent, "zatca.grant_key_delegation")
    if group_parent.parent_id is not None or group_parent.consolidation != Entity.Consolidation.PARENT:
        raise ZatcaError("The super key belongs to the group parent entity.", "المفتاح الأعلى يخص الشركة الأم للمجموعة.",
                         code="not_group_parent")
    if GroupSuperKey.objects.filter(entity=group_parent).exists():
        raise ZatcaError("This group already has a super key.", "للمجموعة مفتاح أعلى بالفعل.", code="exists")
    ref = keystore.get_store().create_super_key(keystore.super_key_path(group_parent.code))
    key = _save(GroupSuperKey(entity=group_parent, key_ref=ref, key_version=keystore.parse_ref(ref)[1], holder=user,
                              created_by=user, source=_source(user)))
    _event(group_parent, None, Action.SUPER_KEY_REGISTERED, user, key_ref=ref)
    return key


def _super_key_for(entity):
    parent = access.group_parent_of(entity)
    key = GroupSuperKey.objects.filter(entity=parent).first()
    if key is None:
        raise ZatcaError("The group super key is not registered yet.", "لم يُسجَّل المفتاح الأعلى للمجموعة بعد.",
                         code="no_super_key")
    return key


def create_entity_master_key(entity, *, user):
    """R-50 b / AT-70: the entity's own master key under its group's super key; the MP only."""
    access.require(user, entity, "zatca.grant_key_delegation")
    if EntityMasterKey.objects.filter(entity=entity).exists():
        raise ZatcaError("This entity already has a master key; rotate it instead.", "للكيان مفتاح رئيسي بالفعل.",
                         code="exists")
    super_key = _super_key_for(entity)
    try:
        ref = keystore.get_store().create_master_key(keystore.master_key_path(entity.code), super_key.key_ref)
    except SecretStoreUnavailable as exc:
        raise ZatcaError(exc.en, exc.ar, code="secret_store_unavailable") from None
    mk = _save(EntityMasterKey(entity=entity, key_ref=ref, key_version=keystore.parse_ref(ref)[1],
                               protected_by=super_key, created_by=user, source=_source(user)))
    _event(entity, None, Action.MASTER_KEY_CREATED, user, key_ref=ref)
    return mk


def rotate_entity_master_key(entity, *, user):
    """A new version of the entity master key (needs the super key: the MP only, AT-71)."""
    access.require(user, entity, "zatca.grant_key_delegation")
    mk = EntityMasterKey.objects.filter(entity=entity).first()
    if mk is None:
        raise ZatcaError("This entity has no master key.", "ليس للكيان مفتاح رئيسي.", code="no_master_key")
    ref = keystore.get_store().rotate_master_key(keystore.parse_ref(mk.key_ref)[0], mk.protected_by.key_ref)
    mk.key_ref, mk.key_version = ref, keystore.parse_ref(ref)[1]
    _save(mk)
    _event(entity, None, Action.MASTER_KEY_ROTATED, user, key_ref=ref)
    return mk


def grant_key_delegation(entity, *, user, custodian, scope, openbao_policy_ref, valid_until=None):
    """R-41 / access R17, R51: the MP delegates key custody for **one** entity to a named ZATCA admin."""
    access.require(user, entity, "zatca.grant_key_delegation")
    if scope == "unseal":  # withdrawn by D-12
        raise ZatcaError("The unseal scope is withdrawn (D-12); delegate an entity's master key instead.",
                         "أُلغي نطاق فك الإغلاق؛ فوّض حفظ المفتاح الرئيسي للكيان بدلًا منه.", code="scope_withdrawn")
    if scope not in Scope.values:
        raise ZatcaError(f"Unknown delegation scope {scope!r}.", f"نطاق التفويض {scope!r} غير معروف.", code="scope")
    if custodian.pk == user.pk:
        access.deny(user, entity, "zatca.grant_key_delegation", "self_grant")
    if not Membership.objects.active(_now()).filter(user=custodian, entity=entity, role__code="zatca_admin").exists():
        _refuse(user, entity, "zatca.grant_key_delegation", "custodian_not_zatca_admin",
                f"The custodian must be a ZATCA administrator of {entity.code} (access R17).",
                f"يجب أن يكون أمين المفاتيح مسؤول ربط مع الهيئة في الكيان {entity.code}.")
    check_no_secret(openbao_policy_ref, "openbao_policy_ref")
    with transaction.atomic():
        d = _save(KeyCustodianDelegation(entity=entity, custodian=custodian, scope=scope, granted_by=user,
                                         valid_until=valid_until, openbao_policy_ref=openbao_policy_ref,
                                         created_by=user, source=_source(user)))
        _event(entity, None, Action.DELEGATION_GRANTED, user, delegation=d.pk, custodian=custodian.pk, scope=scope,
               policy=openbao_policy_ref)
        access_services.log(AccessLog.Event.DELEGATION_GRANTED, actor=user, entity=entity, subject_user=custodian,
                            permission="zatca.grant_key_delegation",
                            details={"zatca_delegation": d.pk, "scope": scope, "policy": openbao_policy_ref})
    return d


def revoke_key_delegation(delegation, *, user, reason):
    d = KeyCustodianDelegation.objects.get(pk=delegation.pk)
    access.require(user, d.entity, "zatca.grant_key_delegation")
    if d.revoked_at is not None:
        raise ZatcaError("This delegation is already revoked.", "هذا التفويض ملغى بالفعل.", code="revoked")
    with transaction.atomic():
        d.revoked_at, d.revoked_by, d.revoke_reason = _now(), user, reason[:300]
        _save(d)
        _event(d.entity, None, Action.DELEGATION_REVOKED, user, delegation=d.pk, custodian=d.custodian_id,
               scope=d.scope)
        access_services.log(AccessLog.Event.DELEGATION_REVOKED, actor=user, entity=d.entity,
                            subject_user=d.custodian, permission="zatca.grant_key_delegation",
                            details={"zatca_delegation": d.pk, "scope": d.scope, "reason": reason[:300]})
    return d


# --- profile (3.1, R-4, R-29, R-36) ---------------------------------------------------------------------

PROFILE_FIELDS = ("go_live_date", "seller_id_scheme", "seller_id_value")


def create_profile(entity, *, user, **fields):
    access.require(user, entity, "zatca.manage_profile")
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:
        raise ZatcaError(f"Unknown profile fields: {sorted(unknown)}.", "حقول غير معروفة في ملف الربط.", code="fields")
    if ZatcaProfile.objects.filter(entity=entity).exists():
        raise ZatcaError("This entity already has a ZATCA profile.", "للكيان ملف ربط بالفعل.", code="exists")
    return _save(ZatcaProfile(entity=entity, created_by=user, source=_source(user), **fields))


def update_profile(profile, *, user, **fields):
    profile = ZatcaProfile.objects.get(pk=profile.pk)
    access.require(user, profile.entity, "zatca.manage_profile")
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:  # environment and is_enabled have their own services
        raise ZatcaError(f"These profile fields change through their own services: {sorted(unknown)}.",
                         "تتغير هذه الحقول من خلال خدماتها الخاصة.", code="fields")
    for name, value in fields.items():
        setattr(profile, name, value)
    return _save(profile)


def _raise_problems(problems, code):
    en = " ".join(p[1] for p in problems)
    ar = " ".join(p[2] for p in problems)
    raise ZatcaError(en, ar, code=code, problems=problems)


def enable_profile(profile, *, user):
    """R-4 / AT-1, AT-48: refused while the entity's VAT number or national address is blank or malformed;
    R-50 b: and until the entity's master key exists."""
    profile = ZatcaProfile.objects.select_related("entity").get(pk=profile.pk)
    access.require(user, profile.entity, "zatca.manage_profile")
    problems = rules.entity_problems(profile.entity)
    if problems:
        _raise_problems(problems, "profile_invalid")
    _master_ref(profile.entity)
    profile.is_enabled = True
    _save(profile)
    _event(profile.entity, None, Action.PROFILE_ENABLED, user)
    return profile


def _production_payload(profile):
    return {"zatca_profile": profile.pk, "environment": "production"}


def request_production_switch(profile, *, user):
    """§7: production needs a second approver (dual control, access R19)."""
    profile = ZatcaProfile.objects.select_related("entity").get(pk=profile.pk)
    return access.request_dual_control(user, profile.entity, SWITCH, profile, _production_payload(profile))


def switch_to_production(profile, *, user, request):
    """R-29: never under test settings (``ZATCA_PRODUCTION_ALLOWED`` off) and never by an agent; R-36: existing
    credentials stay bound to their own environment, so the entity onboards again."""
    profile = ZatcaProfile.objects.select_related("entity").get(pk=profile.pk)
    if access_services.kind_of(user) == Kind.AGENT:
        access.deny(user, profile.entity, SWITCH, "agent_forbidden")
    if not _setting("ZATCA_PRODUCTION_ALLOWED", False):
        raise ZatcaError("Production cannot be selected in this installation (test or development settings, R-29).",
                         "لا يمكن اختيار بيئة الإنتاج في هذا التثبيت (إعدادات اختبار أو تطوير).", code="production_blocked")
    access.require_dual_control(user, profile.entity, SWITCH, request, _production_payload(profile), target=profile)
    old = profile.environment
    profile.environment = "production"
    _save(profile)
    _event(profile.entity, None, Action.ENVIRONMENT_CHANGED, user, old=old, new="production")
    return profile


# --- EGS units and onboarding (3.2, 3.3, R-5 to R-10) -----------------------------------------------------

def create_egs_unit(entity, *, user, code, name_en, name_ar, serial_number, industry, location="",
                    supports_standard=True, supports_simplified=False, branch_name_en="", branch_name_ar=""):
    access.require(user, entity, "zatca.manage_profile")
    location = location or entity.short_address
    missing = [n for n, v in (("code", code), ("name_en", name_en), ("name_ar", name_ar), ("industry", industry),
                              ("location", location)) if not (v or "").strip()]
    if missing or not (supports_standard or supports_simplified):
        raise ZatcaError(f"All CSR fields are mandatory (DTG p.29): {', '.join(missing) or 'invoice type'}.",
                         f"جميع حقول طلب الشهادة إلزامية: {', '.join(missing) or 'نوع الفواتير'}.", code="egs_fields")
    if not rules.SERIAL_RE.match(serial_number or ""):
        raise ZatcaError("The EGS serial number is 1-<solution>|2-<model>|3-<serial> (SFIS p.16).",
                         "الرقم التسلسلي لوحدة الفوترة بالصيغة 1-الحل|2-الطراز|3-التسلسل.", code="serial")
    return _save(EgsUnit(entity=entity, code=code, name_en=name_en, name_ar=name_ar, serial_number=serial_number,
                         industry=industry, location=location, supports_standard=supports_standard,
                         supports_simplified=supports_simplified, branch_name_en=branch_name_en,
                         branch_name_ar=branch_name_ar, created_by=user, source=_source(user)))


def _egs_transition(egs, new):
    if new == EgsStatus.REVOKED:
        egs.status = new
        return
    if new not in EGS_TRANSITIONS.get(egs.status, set()):
        raise ZatcaError(f"An EGS unit cannot go from {egs.status} to {new} (R-5).",
                         f"لا تنتقل وحدة الفوترة من {egs.status} إلى {new}.", code="egs_transition")
    egs.status = new


def _profile_of(entity):
    profile = ZatcaProfile.objects.filter(entity=entity).first()
    if profile is None:
        raise ZatcaError(f"{entity.code} has no ZATCA profile.", f"ليس للكيان {entity.code} ملف ربط مع الهيئة.",
                         code="no_profile")
    return profile


def _new_key_and_csr(egs, environment):
    """R-6 / R-10: a new key pair generated in the ERP; the private key goes only to the secret store."""
    entity = egs.entity
    master = _master_ref(entity)
    key = crypto.generate_private_key()
    csr = crypto.build_csr(
        key, common_name=f"EGS-{egs.code}-{entity.vat_number}",
        org_unit=egs.branch_name_en or egs.branch_name_ar or egs.name_en,
        org_name=entity.legal_name_en or entity.legal_name_ar, serial_number=egs.serial_number,
        vat_number=entity.vat_number, invoice_type=egs.invoice_type_flag, location=egs.location,
        industry=egs.industry, environment=environment)
    ref = keystore.get_store().write_private_key(keystore.private_key_path(entity.code, egs.code),
                                                 crypto.private_key_pem(key), master)
    del key
    return ref, csr


def generate_csr(egs, *, user):
    """AT-2, AT-43, AT-68: draft -> csr_ready; ``private_key_ref`` set; no key material in the DB."""
    egs = EgsUnit.objects.select_related("entity").get(pk=egs.pk)
    entity = egs.entity
    _require_key_action(user, entity, Scope.ONBOARD_RENEW)
    profile = _profile_of(entity)
    if egs.status != EgsStatus.DRAFT:
        _egs_transition(egs, EgsStatus.CSR_READY)  # raises (R-5)
    try:
        ref, csr = _new_key_and_csr(egs, profile.environment)
    except SecretStoreUnavailable as exc:
        raise ZatcaError(exc.en, exc.ar, code="secret_store_unavailable") from None
    with transaction.atomic():
        egs = EgsUnit.objects.select_for_update().get(pk=egs.pk)
        _egs_transition(egs, EgsStatus.CSR_READY)
        egs.private_key_ref = ref
        egs.csr_pem = crypto.csr_pem(csr).decode("ascii")
        _save(egs)
        _event(entity, egs, Action.CSR_GENERATED, user, private_key_ref=ref, environment=profile.environment)
    return egs


def _check_otp(otp):
    if not rules.otp_ok(otp):
        raise ZatcaError("The OTP must be exactly 6 digits (R-7); nothing was sent to ZATCA.",
                         "يجب أن يتكون رمز التحقق من 6 أرقام بالضبط؛ لم يُرسل شيء إلى الهيئة.", code="otp_format")


def _token_and_secret(response):
    token = (response.body or {}).get("binarySecurityToken") or ""
    secret = (response.body or {}).get("secret") or ""
    request_id = str((response.body or {}).get("requestID") or "")
    return token, secret, request_id


def _otp_refused(egs, user, response):
    _event(egs.entity, egs, Action.OTP_REJECTED, user, http_status=response.status)
    if response.status is None:
        raise ZatcaError("ZATCA could not be reached; nothing changed. Try again (with a new OTP once it expires).",
                         "تعذر الوصول إلى الهيئة؛ لم يتغير شيء. أعد المحاولة (برمز تحقق جديد عند انتهاء صلاحيته).",
                         code="zatca_unreachable")
    raise ZatcaError("ZATCA refused the OTP (expired or invalid). Get a new OTP from the Fatoora portal "
                     "(valid for 1 hour); the same OTP is never sent again (R-7).",
                     "رفضت الهيئة رمز التحقق (منتهي أو غير صالح). احصل على رمز جديد من بوابة فاتورة (صالح لمدة ساعة)؛ "
                     "لا يُرسل الرمز نفسه مرة أخرى.", code="otp_rejected")


def _store_credential(egs, kind, environment, response, user):
    token, secret, request_id = _token_and_secret(response)
    cert_b64 = crypto.cert_base64_from_token(token)
    cert = crypto.load_cert_base64(cert_b64)
    ref = keystore.get_store().write_secret(keystore.secret_path(egs.entity.code, egs.code, kind), secret,
                                            _master_ref(egs.entity))
    return EgsCredential(entity=egs.entity, egs_unit=egs, kind=kind, environment=environment,
                         certificate_pem=crypto.cert_pem(cert), secret_ref=ref, zatca_request_id=request_id,
                         valid_from=cert.not_valid_before_utc, valid_to=cert.not_valid_after_utc,
                         created_by=user, source=_source(user))


def enter_otp(egs, otp, *, user):
    """R-7 / AT-3, AT-37, AT-50: the OTP is checked (6 digits) before any call, sent once in the header and
    never stored or logged; csr_ready -> compliance_csid on success."""
    egs = EgsUnit.objects.select_related("entity").get(pk=egs.pk)
    _require_key_action(user, egs.entity, Scope.ONBOARD_RENEW)
    _check_otp(otp)
    if egs.status != EgsStatus.CSR_READY:
        _egs_transition(egs, EgsStatus.COMPLIANCE_CSID)
    profile = _profile_of(egs.entity)
    csr_b64 = base64.b64encode(egs.csr_pem.encode("ascii")).decode("ascii")
    _event(egs.entity, egs, Action.OTP_ENTERED, user)
    response = zatca_client.get_client().compliance_csid(profile.environment, csr_b64, otp)
    del otp
    if response.status != 200 or not _token_and_secret(response)[0]:
        _otp_refused(egs, user, response)
    with transaction.atomic():
        egs = EgsUnit.objects.select_for_update().select_related("entity").get(pk=egs.pk)
        _egs_transition(egs, EgsStatus.COMPLIANCE_CSID)
        credential = _save(_store_credential(egs, EgsCredential.Kind.COMPLIANCE, profile.environment, response, user))
        _save(egs)
        _event(egs.entity, egs, Action.COMPLIANCE_CSID_ISSUED, user, credential=credential.pk,
               request_id=credential.zatca_request_id, secret_ref=credential.secret_ref)
    return egs


def _active_credential(egs, kind):
    return EgsCredential.objects.filter(egs_unit=egs, kind=kind, status=EgsCredential.Status.ACTIVE).first()


def _auth(credential, master):
    secret = keystore.get_store().read_secret(credential.secret_ref, master)
    token = base64.b64encode(crypto.cert_base64(crypto.cert_from_pem(credential.certificate_pem)).encode()).decode()
    return zatca_client.Auth(token, secret)


SAMPLE_BUYER = ubl.Party(name="مشتري عينة الامتثال", vat_number="399999999900003", street="شارع العينة",
                         building_no="1111", district="حي العينة", city="الرياض", postal_code="11111", country="SA")


def _compliance_samples(egs):
    """R-8: flag 1000 -> standard 388, 383, 381; 0100 -> simplified 388, 383, 381; 1100 -> all six."""
    entity = egs.entity
    profile = _profile_of(entity)
    scheme, value = rules.seller_id(entity.cr_number, entity.unified_number, profile.seller_id_scheme,
                                    profile.seller_id_value)
    seller = ubl.Party(name=entity.legal_name_ar, vat_number=entity.vat_number, id_scheme=scheme, id_value=value,
                       street=entity.street_ar or entity.street_en, building_no=entity.building_no,
                       additional_no=entity.additional_no, district=entity.district_ar or entity.district_en,
                       city=entity.city_ar or entity.city_en, postal_code=entity.postal_code, country=entity.country)
    categories = [c for c, on in (("standard", egs.supports_standard), ("simplified", egs.supports_simplified)) if on]
    now = _now().replace(microsecond=0) - timedelta(minutes=1)
    samples = []
    for category in categories:
        invoice_number = ""
        for type_code in ("388", "383", "381"):
            n = len(samples) + 1
            number = f"{egs.code}-COMPLIANCE-{n}"
            invoice_number = invoice_number or number
            line = ubl.Line(1, "عينة امتثال", Decimal("1"), "PCE", Decimal("100.00"), Decimal("0"), Decimal("100.00"),
                            Decimal("15.00"), "S", Decimal("15.00"))
            note = type_code != "388"
            samples.append((f"{category}-{type_code}", ubl.InvoiceData(
                number=number, uuid=str(_uuid5(f"{egs.pk}-{n}-{now.isoformat()}")), issue=now,
                type_code=type_code, category=category, subtype_code=ubl.subtype_code(category, {}), currency="SAR",
                currency_places=2, seller=seller, buyer=SAMPLE_BUYER if category == "standard" else ubl.Party(),
                lines=[line], groups=[ubl.TaxGroup("S", Decimal("15.00"), Decimal("100.00"), Decimal("15.00"))],
                total_net=Decimal("100.00"), total_vat=Decimal("15.00"), total_gross=Decimal("115.00"),
                total_vat_sar=Decimal("15.00"), supply_date=now.date(), payment_means_code="10",
                billing_reference=invoice_number if note else "", reason="إشعار عينة الامتثال" if note else "")))
    return samples


def _uuid5(text):
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, f"urn:almiras:zatca:compliance:{text}")


def _sign_xml(data, icv, pih, key_ref, master, credential, signing_time):
    """Build, hash, sign and assemble one document (R-13 to R-15). Raises qr.QrValueTooLong / QrTooLong."""
    unsigned = ubl.build_unsigned(data, icv, pih)
    invoice_hash = crypto.invoice_hash(unsigned)
    raw = base64.b64decode(invoice_hash)
    signature = keystore.get_store().sign(key_ref, master, raw)
    cert = crypto.cert_from_pem(credential.certificate_pem)
    if not crypto.verify_hash_signature(cert.public_key(), signature, raw):  # key and certificate must match
        raise ZatcaError("The EGS key does not match its certificate; nothing was signed.",
                         "مفتاح وحدة الفوترة لا يطابق شهادتها؛ لم يُوقّع شيء.", code="key_mismatch")
    signature_b64 = base64.b64encode(signature).decode("ascii")
    cert_b64 = crypto.cert_base64(cert)
    qr_b64 = None
    if data.category == "simplified":
        qr_b64 = qr.encode(qr.simplified_values(
            seller_name=data.seller.name, vat_number=data.seller.vat_number,
            timestamp=f"{data.issue_date_text}T{data.issue_time_text}Z", total=ubl.amount(data.total_gross),
            vat_total=ubl.amount(data.total_vat), invoice_hash=invoice_hash, signature_b64=signature_b64,
            public_key_der=crypto.spki_der(cert.public_key()), certificate_signature=cert.signature,
            signature_r_s=crypto.signature_r_s(signature)))
    args = (signing_time.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), crypto.cert_digest(cert_b64),
            crypto.issuer_name(cert, _setting("ZATCA_X509_ISSUER_NAME_STYLE", "rfc2253")), cert.serial_number)
    sp = ubl.signed_properties(*args)
    sp_digest_form = ubl.signed_properties(*args, for_digest=True)
    signed = ubl.assemble(unsigned, ubl.ubl_extensions(invoice_hash, signature_b64, cert_b64, sp, sp_digest_form),
                          qr_b64)
    if crypto.invoice_hash(signed) != invoice_hash:  # the hash of the signed output equals the unsigned input's
        raise ZatcaError("Signing changed the invoice hash; nothing was stored.", "غيّر التوقيع تجزئة الفاتورة.",
                         code="hash_changed")
    return signed, invoice_hash, qr_b64 or ""


def _request_body(invoice_hash, uuid, signed_xml):
    """Clearance / reporting / compliance body ``{"invoiceHash", "uuid", "invoice"}`` (findings §10)."""
    return {"invoiceHash": invoice_hash, "uuid": str(uuid), "invoice": base64.b64encode(signed_xml).decode("ascii")}


def _body_sha256(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _errors(response):
    results = (response.body or {}).get("validationResults") or {}
    return list(results.get("errorMessages") or []), list(results.get("warningMessages") or [])


def run_compliance_checks(egs, *, user):
    """R-8 / AT-4, AT-5: sign the required samples with the compliance CSID and send them to the compliance
    check API. All pass -> compliance_passed; any fails -> back to draft (new OTP and new CSR)."""
    egs = EgsUnit.objects.select_related("entity").get(pk=egs.pk)
    entity = egs.entity
    _require_key_action(user, entity, Scope.ONBOARD_RENEW)
    if egs.status != EgsStatus.COMPLIANCE_CSID:
        _egs_transition(egs, EgsStatus.COMPLIANCE_PASSED)
    profile = _profile_of(entity)
    credential = _active_credential(egs, EgsCredential.Kind.COMPLIANCE)
    master = _master_ref(entity)
    try:
        auth = _auth(credential, master)
    except SecretStoreUnavailable as exc:
        raise ZatcaError(exc.en, exc.ar, code="secret_store_unavailable") from None
    pih, results, passed = crypto.INITIAL_PIH, {}, True
    for icv, (label, data) in enumerate(_compliance_samples(egs), start=1):
        signed, invoice_hash, _ = _sign_xml(data, icv, pih, egs.private_key_ref, master, credential, _now())
        pih = invoice_hash
        response = zatca_client.get_client().compliance_check(profile.environment, auth,
                                                              _request_body(invoice_hash, data.uuid, signed))
        errors, _ = _errors(response)
        ok = response.status in (200, 202) and not errors
        results[label] = {"http_status": response.status, "passed": ok,
                          "errors": [e.get("code", "") for e in errors][:20]}
        passed = passed and ok
    with transaction.atomic():
        egs = EgsUnit.objects.select_for_update().get(pk=egs.pk)
        if passed:
            _egs_transition(egs, EgsStatus.COMPLIANCE_PASSED)
            _save(egs)
            _event(entity, egs, Action.COMPLIANCE_CHECKS_PASSED, user, results=results)
        else:
            _egs_transition(egs, EgsStatus.DRAFT)
            egs.csr_pem = ""
            _save(egs)
            credential.status = EgsCredential.Status.SUPERSEDED
            _save(credential)
            _event(entity, egs, Action.COMPLIANCE_CHECKS_FAILED, user, results=results)
    if not passed:
        failed = ", ".join(k for k, v in results.items() if not v["passed"])
        raise ZatcaError(f"Compliance checks failed ({failed}); onboarding restarts with a new CSR and a new OTP (R-8).",
                         f"لم تجتز فحوص الامتثال ({failed})؛ يبدأ التسجيل من جديد بطلب شهادة ورمز تحقق جديدين.",
                         code="compliance_failed")
    return egs


def request_production_csid(egs, *, user):
    """R-8 / AT-5, AT-6: only from compliance_passed; compliance_passed -> active with the production CSID."""
    egs = EgsUnit.objects.select_related("entity").get(pk=egs.pk)
    entity = egs.entity
    _require_key_action(user, entity, Scope.ONBOARD_RENEW)
    if egs.status != EgsStatus.COMPLIANCE_PASSED:
        _egs_transition(egs, EgsStatus.ACTIVE)  # raises (R-5)
    profile = _profile_of(entity)
    compliance = _active_credential(egs, EgsCredential.Kind.COMPLIANCE)
    try:
        auth = _auth(compliance, _master_ref(entity))
    except SecretStoreUnavailable as exc:
        raise ZatcaError(exc.en, exc.ar, code="secret_store_unavailable") from None
    response = zatca_client.get_client().production_csid(profile.environment, auth, compliance.zatca_request_id)
    if response.status != 200 or not _token_and_secret(response)[0]:
        raise ZatcaError(f"ZATCA did not issue the production CSID (HTTP {response.status}).",
                         f"لم تُصدر الهيئة شهادة الإنتاج (HTTP {response.status}).", code="pcsid_refused")
    with transaction.atomic():
        egs = EgsUnit.objects.select_for_update().select_related("entity").get(pk=egs.pk)
        _egs_transition(egs, EgsStatus.ACTIVE)
        production = _save(_store_credential(egs, EgsCredential.Kind.PRODUCTION, profile.environment, response, user))
        compliance.status, compliance.replaced_by = EgsCredential.Status.SUPERSEDED, production
        _save(compliance)
        _save(egs)
        _event(entity, egs, Action.PCSID_ISSUED, user, credential=production.pk, secret_ref=production.secret_ref)
    return egs


def renew_production_csid(egs, otp, *, user):
    """R-10 / AT-9: new OTP + new CSR with a **new key pair**; the old credential becomes superseded."""
    egs = EgsUnit.objects.select_related("entity").get(pk=egs.pk)
    entity = egs.entity
    _require_key_action(user, entity, Scope.ONBOARD_RENEW)
    _check_otp(otp)
    if egs.status not in (EgsStatus.ACTIVE, EgsStatus.RENEWAL_DUE):
        raise ZatcaError("Only an active unit (or one due for renewal) is renewed (R-5, R-10).",
                         "تُجدَّد الوحدة النشطة أو المستحقة للتجديد فقط.", code="egs_transition")
    profile = _profile_of(entity)
    old = _active_credential(egs, EgsCredential.Kind.PRODUCTION)
    try:
        master = _master_ref(entity)
        auth = _auth(old, master)
        ref, csr = _new_key_and_csr(egs, profile.environment)
    except SecretStoreUnavailable as exc:
        raise ZatcaError(exc.en, exc.ar, code="secret_store_unavailable") from None
    _event(entity, egs, Action.OTP_ENTERED, user)
    response = zatca_client.get_client().renew_production_csid(profile.environment, auth, crypto.csr_base64(csr), otp)
    del otp
    if response.status != 200 or not _token_and_secret(response)[0]:
        _otp_refused(egs, user, response)
    new_public = keystore.get_store().public_key_der(ref, master)
    with transaction.atomic():
        egs = EgsUnit.objects.select_for_update().select_related("entity").get(pk=egs.pk)
        new = _store_credential(egs, EgsCredential.Kind.PRODUCTION, profile.environment, response, user)
        if crypto.spki_der(crypto.cert_from_pem(new.certificate_pem).public_key()) != new_public:
            raise ZatcaError("The renewed certificate is not for the new key pair; nothing changed.",
                             "الشهادة المجددة لا تخص زوج المفاتيح الجديد؛ لم يتغير شيء.", code="key_mismatch")
        old.status = EgsCredential.Status.SUPERSEDED
        _save(old)
        _save(new)
        old.replaced_by = new
        _save(old)
        _egs_transition(egs, EgsStatus.ACTIVE)
        egs.private_key_ref, egs.csr_pem = ref, crypto.csr_pem(csr).decode("ascii")
        _save(egs)
        _event(entity, egs, Action.PCSID_RENEWED, user, credential=new.pk, replaces=old.pk, private_key_ref=ref)
    return egs


def revoke_egs_unit(egs, *, user, reason):
    """Any -> revoked (R-5); its credentials become revoked. Needs a ``revoke`` delegation (R-41)."""
    egs = EgsUnit.objects.select_related("entity").get(pk=egs.pk)
    _require_key_action(user, egs.entity, Scope.REVOKE)
    with transaction.atomic():
        _revoke_unit(egs)
        _event(egs.entity, egs, Action.REVOKED, user, reason=reason[:300])
    return egs


def _revoke_unit(egs):
    egs = EgsUnit.objects.select_for_update().get(pk=egs.pk)
    for credential in EgsCredential.objects.filter(egs_unit=egs, status=EgsCredential.Status.ACTIVE):
        credential.status = EgsCredential.Status.REVOKED
        _save(credential)
    _egs_transition(egs, EgsStatus.REVOKED)
    _save(egs)


VAT_CHANGES = ("deregistered", "suspended", "joined_vat_group")


def record_vat_status_change(entity, *, user, change, reason=""):
    """R-45 / AT-57: ZATCA revokes the entity's CSIDs; all its credentials and EGS units become revoked."""
    access.require(user, entity, "zatca.manage_profile")
    if change not in VAT_CHANGES:
        raise ZatcaError(f"Unknown VAT status change {change!r}.", f"تغيير الحالة الضريبية {change!r} غير معروف.",
                         code="vat_change")
    with transaction.atomic():
        for egs in EgsUnit.objects.filter(entity=entity).exclude(status=EgsStatus.REVOKED):
            _revoke_unit(egs)
        _event(entity, None, Action.VAT_STATUS_CHANGED, user, change=change, reason=reason[:300])
    return entity


def refresh_credential_statuses(*, user, now=None):
    """R-10 / AT-7: daily job (ZATCA queue user). ``renewal_due`` N days (``ZATCA_RENEWAL_DAYS``, default 30)
    before ``valid_to``, admins alerted; an expired credential expires its unit (nothing signs with it)."""
    now = now or _now()
    days = int(_setting("ZATCA_RENEWAL_DAYS", 30))
    changed = []
    for entity in access.entities_for(user, PROCESS):
        access.require(user, entity, PROCESS)
        for egs in EgsUnit.objects.filter(entity=entity, status__in=(EgsStatus.ACTIVE, EgsStatus.RENEWAL_DUE)):
            credential = _active_credential(egs, EgsCredential.Kind.PRODUCTION)
            if credential is None:
                continue
            with transaction.atomic():
                egs = EgsUnit.objects.select_for_update().get(pk=egs.pk)
                if credential.valid_to <= now:
                    credential.status = EgsCredential.Status.EXPIRED
                    _save(credential)
                    _egs_transition(egs, EgsStatus.EXPIRED)
                    _save(egs)
                    _alert(entity, _zatca_admins(entity), "credential_expired",
                           f"The production CSID of EGS unit {egs.code} expired; documents are not signed until "
                           "the unit is onboarded again.",
                           f"انتهت شهادة الإنتاج لوحدة الفوترة {egs.code}؛ لا توقّع المستندات حتى يُعاد تسجيلها.", egs=egs)
                    changed.append(egs)
                elif egs.status == EgsStatus.ACTIVE and credential.valid_to - timedelta(days=days) <= now:
                    _egs_transition(egs, EgsStatus.RENEWAL_DUE)
                    _save(egs)
                    _alert(entity, _zatca_admins(entity), "renewal_due",
                           f"The production CSID of EGS unit {egs.code} expires on {credential.valid_to:%Y-%m-%d}; "
                           "renew it with a new OTP.",
                           f"تنتهي شهادة الإنتاج لوحدة الفوترة {egs.code} في {credential.valid_to:%Y-%m-%d}؛ "
                           "جدّدها برمز تحقق جديد.", egs=egs)
                    changed.append(egs)
    return changed


# --- documents: hand-off (R-28, R-32), stamping (R-9 to R-16), submission (R-21 to R-27, R-44) -------------

def _authorise_submit(user, doc):
    """Who hands a document to zatca: the approver who issued it (its used approval), the ZATCA queue user
    (``zatca.process_queue``) or a ``zatca_admin`` resubmitting (invoicing R79, D-15). Agents never (R-34)."""
    entity = doc.entity
    kind = access_services.kind_of(user)
    if kind == Kind.SYSTEM:
        return access.require(user, entity, PROCESS)
    if kind == Kind.AGENT:
        access.deny(user, entity, PROCESS, "agent_forbidden")
    if user.pk in access.recorded_approvers(entity, "invoicing.approve_sales_document", doc, used=True):
        return None
    grant = access.require(user, entity, "zatca.retry_failed")
    if not Membership.objects.filter(pk__in=grant.membership_ids, role__code="zatca_admin").exists():
        access.deny(user, entity, "zatca.retry_failed", "no_membership")
    return grant


def _issue_moment(doc):
    """The document's issue moment (invoicing R78: approval date and time in Riyadh), stored in UTC."""
    return datetime.combine(doc.issue_date, doc.issue_time or time(0, 0), tzinfo=RIYADH).astimezone(dt_timezone.utc)


def _retention_class(doc):
    if doc.retention_class == "capital_asset_real_estate" or doc.lines.filter(income_account__code="1510").exists():
        return EInvoice.Retention.REAL_ESTATE
    return doc.retention_class or EInvoice.Retention.STANDARD


def _retention_until(einv, doc, acquisition_date=None):
    computed = rules.retention_until(einv.retention_class, doc.issue_date, doc.entity.vat_return_period,
                                     acquisition_date=acquisition_date or einv.acquisition_date,
                                     adjustment_years=_setting("ZATCA_MOVABLE_ADJUSTMENT_YEARS", None))
    return max(d for d in (computed, doc.retention_until, einv.retention_until) if d)


def _pick_egs(entity, category):
    field = "supports_standard" if category == EInvoice.Category.STANDARD else "supports_simplified"
    return (EgsUnit.objects.filter(entity=entity, status__in=(EgsStatus.ACTIVE, EgsStatus.RENEWAL_DUE),
                                   **{field: True}).order_by("code").first())


def _go_live_ok(profile, today):
    return profile is not None and profile.is_enabled and profile.go_live_date is not None and profile.go_live_date <= today


def submit_document(document, user):
    """invoicing's hand-off (port ``submit_document``, invoicing R40): one ``EInvoice`` per issued document.

    No profile: the entity is not on ZATCA Phase 2 in this ERP; the document is only recorded as submitted
    (``pending``), as the default port did. Profile not enabled or not live (R-28): held ``not_live`` with a
    message saying why; never submitted. Otherwise ``pending`` for the queue worker.
    """
    from invoicing import zatca_port
    from invoicing.models import SalesDocument

    doc = SalesDocument.objects.select_related("entity", "original_document").get(pk=document.pk)
    _authorise_submit(user, doc)
    if doc.status != SalesDocument.Status.ISSUED:
        raise ZatcaError("Only issued sales documents are handed to ZATCA (R-32).",
                         "تُسلَّم مستندات المبيعات الصادرة فقط إلى الهيئة.", code="not_issued")
    profile = ZatcaProfile.objects.filter(entity=doc.entity).first()
    if profile is None:
        zatca_port.record_submission(doc)
        return None
    with transaction.atomic():
        einv = EInvoice.objects.filter(source_document=doc).first()
        if einv is None:
            category = doc.zatca_profile
            original = EInvoice.objects.filter(source_document_id=doc.original_document_id).first() \
                if doc.original_document_id else None
            issue = _issue_moment(doc)
            einv = EInvoice(
                entity=doc.entity, source_document=doc, uuid=doc.uuid, zatca_category=category,
                type_code=doc.zatca_type_code, billing_reference=original, issue_datetime=issue,
                api=EInvoice.Api.CLEARANCE if category == EInvoice.Category.STANDARD else EInvoice.Api.REPORTING,
                reporting_deadline=issue + timedelta(hours=24) if category == EInvoice.Category.SIMPLIFIED else None,
                retention_class=_retention_class(doc), created_by=user, source=_source(user),
                egs_unit=_pick_egs(doc.entity, category))
            einv.retention_until = _retention_until(einv, doc)
            if not _go_live_ok(profile, _now().astimezone(RIYADH).date()):
                einv.hold_reason = EInvoice.Hold.NOT_LIVE
            _save(einv)
            if einv.hold_reason == EInvoice.Hold.NOT_LIVE:
                code, en, ar = local("not_live")
                ValidationMessage.objects.create(entity=einv.entity, einvoice=einv, level=ValidationMessage.Level.INFO,
                                                 origin=ValidationMessage.Origin.LOCAL_RULES, code=code,
                                                 message_en=en, message_ar=ar)
    zatca_port.record_submission(doc)
    if einv.hold_reason == EInvoice.Hold.NOT_LIVE:
        _sync_invoicing(einv)
    return einv


def _invoice_data(einv):
    """The XML data of an issued document, from its frozen snapshot (invoicing R15, R72)."""
    doc = einv.source_document
    profile = ZatcaProfile.objects.filter(entity=einv.entity).first()
    scheme, value = rules.seller_id(doc.seller_cr_number, doc.seller_unified_number,
                                    profile.seller_id_scheme if profile else "",
                                    profile.seller_id_value if profile else "")
    seller = ubl.Party(
        name=doc.seller_legal_name_ar or doc.seller_legal_name_en, vat_number=doc.seller_vat_number,
        id_scheme=scheme, id_value=value, street=doc.seller_street_ar or doc.seller_street_en,
        building_no=doc.seller_building_no, additional_no=doc.seller_additional_no,
        district=doc.seller_district_ar or doc.seller_district_en, city=doc.seller_city_ar or doc.seller_city_en,
        postal_code=doc.seller_postal_code, country=doc.seller_country)
    buyer = ubl.Party(
        name=doc.buyer_name_ar or doc.buyer_name_en, vat_number=doc.buyer_vat_number,
        id_scheme=doc.buyer_other_id_scheme, id_value=doc.buyer_other_id,
        street=doc.buyer_street_ar or doc.buyer_street_en, building_no=doc.buyer_building_no,
        additional_no=doc.buyer_additional_no, district=doc.buyer_district_ar or doc.buyer_district_en,
        city=doc.buyer_city_ar or doc.buyer_city_en, postal_code=doc.buyer_postal_code, country=doc.buyer_country)
    lines = [ubl.Line(line.line_no, line.description_ar or line.description_en, line.quantity, line.unit_code,
                      line.unit_price, line.line_discount, line.net_amount, line.vat_amount, line.vat_category,
                      line.vat_rate.rate if line.vat_rate_id else Decimal("0"))
             for line in doc.lines.select_related("vat_rate").order_by("line_no")]
    from invoicing.models import VatExemptionReason

    groups = []
    for g in doc.vat_breakdown or []:
        codes = tuple(g.get("exemption_reasons") or ())
        text = ""
        if codes:
            texts = g.get("exemption_texts") or []
            if codes[0] == "VATEX-SA-OOS" and texts:
                text = texts[0].get("ar") or texts[0].get("en") or ""
            else:
                reason = VatExemptionReason.objects.filter(code=codes[0]).first()
                text = reason.name_en if reason else ""
        groups.append(ubl.TaxGroup(g["category"], Decimal(g["rate"]), Decimal(g["taxable"]), Decimal(g["vat"]),
                                   codes, text))
    flags = {"third_party": doc.flag_third_party, "nominal": doc.flag_nominal, "export": doc.flag_export,
             "summary": doc.flag_summary, "self_billed": doc.flag_self_billed}
    original = einv.billing_reference
    return ubl.InvoiceData(
        number=doc.number, uuid=str(einv.uuid), issue=einv.issue_datetime.astimezone(dt_timezone.utc),
        type_code=einv.type_code, category=einv.zatca_category, subtype_code=ubl.subtype_code(einv.zatca_category, flags),
        currency=doc.currency_id, currency_places=doc.currency.minor_units, seller=seller, buyer=buyer, lines=lines,
        groups=groups, total_net=doc.total_net, total_vat=doc.total_vat, total_gross=doc.total_gross,
        total_vat_sar=doc.total_vat_sar, supply_date=doc.supply_date, supply_end_date=doc.supply_end_date,
        payment_means_code=doc.payment_means_code,
        billing_reference=original.source_document.number if original else "",
        billing_reference_entity_ok=original is None or original.entity_id == einv.entity_id,
        billing_reference_category=original.zatca_category if original else "",
        reason=doc.reason_ar.strip(), flags=flags, retention_class=einv.retention_class,
        acquisition_date=einv.acquisition_date)


class _LocalProblems(Exception):
    def __init__(self, problems, origin=ValidationMessage.Origin.LOCAL_RULES):
        self.problems, self.origin = problems, origin
        super().__init__("; ".join(p[1] for p in problems))


def _transition(einv, new):
    if new not in TRANSITIONS.get(einv.status, set()):
        raise ZatcaError(f"An e-invoice cannot go from {einv.status} to {new} (R-20).",
                         f"لا تنتقل الفاتورة الإلكترونية من {einv.status} إلى {new}.", code="transition")
    einv.status = new


def _messages(einv, problems, *, origin, level=ValidationMessage.Level.ERROR, attempt=None):
    for code, en, ar in problems:
        ValidationMessage.objects.create(entity=einv.entity, einvoice=einv, attempt=attempt, level=level, origin=origin,
                                         code=code, message_en=en, message_ar=ar)


def _hold(einv, reason, problems, origin=ValidationMessage.Origin.LOCAL_RULES, *, retry_at=None):
    with transaction.atomic():
        einv = EInvoice.objects.select_for_update().get(pk=einv.pk)
        last = einv.messages.order_by("-id").values_list("code", flat=True).first()
        if einv.hold_reason != reason or last != (problems[0][0] if problems else None):
            _messages(einv, problems, origin=origin)
            if reason == EInvoice.Hold.LOCAL_VALIDATION:
                _alert(einv.entity, _zatca_admins(einv.entity), "local_validation",
                       f"Document {einv.source_document.number} failed the local ZATCA checks and is held.",
                       f"لم يجتز المستند {einv.source_document.number} الفحص المحلي للهيئة وتوقف.", einvoice=einv)
        einv.hold_reason, einv.next_attempt_at = reason, retry_at
        _save(einv)
    _sync_invoicing(einv)
    return einv


def _signing_credential(einv, egs, profile, now):
    """R-2, R-9, R-10, R-36, R-45: the unit and credential that may sign this document now."""
    if egs.entity_id != einv.entity_id:  # R-2 / AT-16
        raise ZatcaError("A document is signed only by an EGS unit of its own entity (R-2).",
                         "يوقّع المستند وحدة فوترة تابعة لكيانه فقط.", code="other_entity_egs")
    if not profile or not profile.is_enabled:
        raise ZatcaError("The entity's ZATCA profile is not enabled.", "ملف الربط للكيان غير مفعّل.", code="profile")
    if egs.status not in (EgsStatus.ACTIVE, EgsStatus.RENEWAL_DUE):
        raise ZatcaError(f"EGS unit {egs.code} is {egs.status}; only an active unit signs (R-9).",
                         f"وحدة الفوترة {egs.code} في حالة {egs.status}؛ توقّع الوحدة النشطة فقط.", code="egs_status")
    supported = egs.supports_standard if einv.zatca_category == EInvoice.Category.STANDARD else egs.supports_simplified
    if not supported:
        raise ZatcaError("This EGS unit is not onboarded for this category.", "وحدة الفوترة غير مسجلة لهذه الفئة.",
                         code="egs_category")
    credential = _active_credential(egs, EgsCredential.Kind.PRODUCTION)
    if credential is None or not (credential.valid_from <= now < credential.valid_to):  # R-10 / AT-8
        raise ZatcaError("The unit has no valid production CSID (expired or missing); nothing is signed (R-9, R-10).",
                         "لا تملك الوحدة شهادة إنتاج صالحة (منتهية أو مفقودة)؛ لا يُوقَّع شيء.", code="credential")
    if credential.environment != profile.environment:  # R-36 / AT-38
        raise ZatcaError(f"The credential was issued in {credential.environment}, the profile is {profile.environment};"
                         " onboard again in this environment (R-36).",
                         f"صدرت الشهادة في بيئة {credential.environment} والملف في بيئة {profile.environment}؛ "
                         "أعد التسجيل في هذه البيئة.", code="environment")
    return credential


def _xsd_problems(signed):
    path = _setting("ZATCA_UBL_INVOICE_XSD", "")
    if not path:
        return []
    schema = etree.XMLSchema(etree.parse(path))
    if schema.validate(etree.fromstring(signed, crypto._parser())):
        return []
    first = schema.error_log.last_error
    code, en, ar = local("xsd")
    return [(code, f"{en} {first.message if first else ''}".strip(), ar)]


def stamp_document(einvoice, *, user, now=None):
    """Queue worker only (§7): ICV, PIH, hash, signature and QR in one transaction under the EGS row lock
    (R-11, R-12). Local problems stop the document at ``pending`` and consume no ICV (R-16); so does an
    unavailable secret store (R-47)."""
    einv = EInvoice.objects.select_related("entity").get(pk=einvoice.pk)
    access.require(user, einv.entity, PROCESS)
    return _stamp(einv, user, now or _now())


def _stamp(einv, user, now):
    if einv.status != Status.PENDING or einv.hold_reason not in RETRIED_HOLDS:
        return einv
    profile = ZatcaProfile.objects.filter(entity=einv.entity).first()
    egs = einv.egs_unit or _pick_egs(einv.entity, einv.zatca_category)
    if egs is None:
        code, en, ar = local("no_egs")
        return _hold(einv, EInvoice.Hold.NO_EGS, [(code, en, ar)], retry_at=now + _backoff(1))
    data = _invoice_data(einv)
    problems = rules.document_problems(data)
    if problems:
        return _hold(einv, EInvoice.Hold.LOCAL_VALIDATION, problems)
    try:
        with transaction.atomic():
            egs = EgsUnit.objects.select_for_update().get(pk=egs.pk)
            einv = EInvoice.objects.select_for_update().select_related("entity").get(pk=einv.pk)
            if einv.status != Status.PENDING:
                return einv
            credential = _signing_credential(einv, egs, profile, now)
            master = _master_ref(einv.entity)
            icv, pih = egs.last_icv + 1, egs.last_invoice_hash  # R-11, R-12
            try:
                signed, invoice_hash, qr_b64 = _sign_xml(data, icv, pih, egs.private_key_ref, master, credential, now)
            except qr.QrValueTooLong:
                raise _LocalProblems([local("qr_value")]) from None
            except qr.QrTooLong:
                raise _LocalProblems([local("qr_length")]) from None
            xsd = _xsd_problems(signed)
            if xsd:
                raise _LocalProblems(xsd, ValidationMessage.Origin.LOCAL_XSD)
            if len(signed) > int(_setting("ZATCA_MAX_XML_BYTES", 10 * 1024 * 1024)):  # R-16 / AT-55
                raise _LocalProblems([local("xml_size")])
            einv.egs_unit = egs
            einv.icv, einv.pih, einv.invoice_hash = icv, pih, invoice_hash
            einv.signed_xml, einv.qr_tlv, einv.signing_time = signed.decode("utf-8"), qr_b64, now
            einv.subtype_code, einv.hold_reason, einv.next_attempt_at = data.subtype_code, "", None
            einv.retention_until = _retention_until(einv, einv.source_document)
            _transition(einv, Status.STAMPED)
            _save(einv)
            egs.last_icv, egs.last_invoice_hash = icv, invoice_hash
            _save(egs)
            _transition(einv, Status.QUEUED)
            _save(einv)
    except _LocalProblems as exc:
        return _hold(einv, EInvoice.Hold.LOCAL_VALIDATION, exc.problems, exc.origin)
    except SecretStoreUnavailable as exc:
        _secret_store_down(einv.entity, exc)
        einv = EInvoice.objects.get(pk=einv.pk)
        einv.next_attempt_at = now + _backoff(1)
        return _save(einv)
    except (SecretStoreRefused, ZatcaError) as exc:
        en, ar = getattr(exc, "en", str(exc)), getattr(exc, "ar", "")
        return _hold(einv, EInvoice.Hold.EGS_NOT_READY, [(getattr(exc, "code", None) or "egs_not_ready", en, ar)],
                     retry_at=now + _backoff(1))
    _secret_store_up(einv.entity)
    _sync_invoicing(einv)
    return einv


def _backoff(failures):
    """R-24: 1 min, doubling, capped at 1 h (settings ``ZATCA_RETRY_BASE_SECONDS``, ``ZATCA_RETRY_CAP_SECONDS``)."""
    base = int(_setting("ZATCA_RETRY_BASE_SECONDS", 60))
    cap = int(_setting("ZATCA_RETRY_CAP_SECONDS", 3600))
    return timedelta(seconds=min(cap, base * 2 ** max(0, failures - 1)))


def _store_event_state(entity):
    last = OnboardingEvent.objects.filter(
        entity=entity, action__in=(Action.SECRET_STORE_UNAVAILABLE, Action.SECRET_STORE_AVAILABLE)).order_by("-id").first()
    return last.action if last else None


def _secret_store_down(entity, exc):
    """R-47: at the first failure, one event and alerts (en/ar) to the entity's ZATCA admins, the MP and the
    entity's ``master_key`` custodians."""
    if _store_event_state(entity) == Action.SECRET_STORE_UNAVAILABLE:
        return
    _event(entity, None, Action.SECRET_STORE_UNAVAILABLE, _queue_user(), reason=getattr(exc, "en", "unavailable"))
    recipients = _zatca_admins(entity) + _managing_partners(entity) + _master_key_custodians(entity)
    _alert(entity, recipients, "secret_store_unavailable",
           f"The secret store (or {entity.code}'s master key) is unavailable: {entity.code} documents are not signed "
           "or submitted until it is back. Simplified invoices must be reported within 24 hours.",
           f"مخزن الأسرار (أو المفتاح الرئيسي للكيان {entity.code}) غير متاح: لا توقّع مستندات الكيان ولا تُرسل حتى "
           "يعود. يجب الإبلاغ عن الفواتير المبسطة خلال 24 ساعة.")


def _secret_store_up(entity):
    if _store_event_state(entity) == Action.SECRET_STORE_UNAVAILABLE:
        _event(entity, None, Action.SECRET_STORE_AVAILABLE, _queue_user())


def _classify(api, response):
    status = response.status
    errors, warnings = _errors(response)
    if status is None or status == 429 or (status >= 500):
        return SubmissionAttempt.Outcome.TRANSIENT_ERROR
    if status == 303 and api == EInvoice.Api.CLEARANCE:
        return SubmissionAttempt.Outcome.CLEARANCE_DISABLED
    if status == 200 and not errors:
        return SubmissionAttempt.Outcome.ACCEPTED_WITH_WARNINGS if warnings else SubmissionAttempt.Outcome.ACCEPTED
    if status == 202 and not errors:
        return SubmissionAttempt.Outcome.ACCEPTED_WITH_WARNINGS
    if status == 400 or errors:
        return SubmissionAttempt.Outcome.REJECTED
    return SubmissionAttempt.Outcome.PERMANENT_ERROR


def _zatca_messages(einv, attempt, response):
    errors, warnings = _errors(response)
    for level, items in ((ValidationMessage.Level.ERROR, errors), (ValidationMessage.Level.WARNING, warnings)):
        for item in items:
            code = str(item.get("code") or "")[:100]
            ValidationMessage.objects.create(
                entity=einv.entity, einvoice=einv, attempt=attempt, level=level, origin=ValidationMessage.Origin.ZATCA,
                code=code or "zatca", category=str(item.get("category") or "")[:100],
                message_en=str(item.get("message") or code), message_ar=arabic_for(code))


def send_einvoice(einvoice, *, user, api=None, now=None):
    """Submit one queued document (queue worker only). Standard documents go to clearance and simplified ones
    to reporting; a forced wrong API is refused (R-21, AT-24); after HTTP 303 the same signed XML goes to
    reporting (R-44). Retries resend the same body (R-24)."""
    einv = EInvoice.objects.select_related("entity").get(pk=einvoice.pk)
    access.require(user, einv.entity, PROCESS)
    route = api or einv.api
    if route == EInvoice.Api.REPORTING and einv.zatca_category == EInvoice.Category.STANDARD \
            and not einv.clearance_disabled:
        raise ZatcaError("A standard document goes to the clearance API; reporting only after ZATCA disabled "
                         "clearance (HTTP 303) (R-21).",
                         "يُرسل المستند القياسي إلى واجهة الاعتماد؛ والإبلاغ فقط بعد تعطيل الهيئة للاعتماد.",
                         code="wrong_api")
    if route == EInvoice.Api.CLEARANCE and einv.zatca_category == EInvoice.Category.SIMPLIFIED:
        raise ZatcaError("A simplified document goes to the reporting API (R-21).",
                         "يُرسل المستند المبسط إلى واجهة الإبلاغ.", code="wrong_api")
    return _send(einv, user, now or _now())


def _send(einv, user, now):
    with transaction.atomic():
        einv = EInvoice.objects.select_for_update().select_related("entity").get(pk=einv.pk)
        if einv.status != Status.QUEUED:
            return einv
        _transition(einv, Status.SUBMITTING)
        _save(einv)
    einv = _post(einv, user, now, einv.api)
    _sync_invoicing(einv)
    return einv


def _post(einv, user, now, api):
    profile = ZatcaProfile.objects.get(entity=einv.entity)
    body = _request_body(einv.invoice_hash, einv.uuid, einv.signed_xml.encode("utf-8"))
    started = _now()
    credential = _active_credential(einv.egs_unit, EgsCredential.Kind.PRODUCTION)
    try:
        if credential is None:
            raise SecretStoreRefused("No active production credential.", "لا توجد شهادة إنتاج نشطة.")
        auth = _auth(credential, _master_ref(einv.entity))
        method = zatca_client.get_client().clearance if api == EInvoice.Api.CLEARANCE \
            else zatca_client.get_client().reporting
        response = method(profile.environment, auth, body)
    except SecretStoreUnavailable as exc:  # R-47: a transient failure under R-24
        _secret_store_down(einv.entity, exc)
        response = zatca_client.ApiResponse(None, {}, "secret_store_unavailable")
    except (SecretStoreRefused, ZatcaError) as exc:
        response = zatca_client.ApiResponse(None, {}, f"credential_unavailable: {getattr(exc, 'en', exc)}")
    else:
        _secret_store_up(einv.entity)
    outcome = _classify(api, response)
    with transaction.atomic():
        einv = EInvoice.objects.select_for_update().select_related("entity").get(pk=einv.pk)
        attempt = SubmissionAttempt.objects.create(
            entity=einv.entity, einvoice=einv, attempt_no=einv.attempts.count() + 1, api=api,
            environment=profile.environment, request_sha256=_body_sha256(body), http_status=response.status,
            response_json=response.body or ({"error": response.error} if response.error else None),
            outcome=outcome, started_at=started, finished_at=_now(), created_by=user)
        _zatca_messages(einv, attempt, response)
        O = SubmissionAttempt.Outcome
        if outcome in (O.ACCEPTED, O.ACCEPTED_WITH_WARNINGS):
            warned = outcome == O.ACCEPTED_WITH_WARNINGS
            if einv.clearance_disabled:
                _transition(einv, Status.REPORTED_CLEARANCE_DISABLED)
            elif api == EInvoice.Api.CLEARANCE:
                cleared = (response.body or {}).get("clearedInvoice") or ""
                einv.cleared_xml = base64.b64decode(cleared).decode("utf-8") if cleared else ""
                if einv.cleared_xml:
                    einv.qr_tlv = einv.qr_tlv or ubl.qr_from_xml(einv.cleared_xml.encode("utf-8"))
                _transition(einv, Status.CLEARED_WITH_WARNINGS if warned else Status.CLEARED)
            else:
                _transition(einv, Status.REPORTED_WITH_WARNINGS if warned else Status.REPORTED)
        elif outcome == O.CLEARANCE_DISABLED:
            einv.clearance_disabled, einv.api = True, EInvoice.Api.REPORTING
            _save(einv)
            _alert(einv.entity, _zatca_admins(einv.entity), "clearance_disabled",
                   f"ZATCA disabled clearance (HTTP 303): {einv.source_document.number} is sent to reporting. It is not "
                   "issued to the buyer or posted until the tax adviser signs off OQ-7.",
                   f"عطّلت الهيئة الاعتماد (HTTP 303): يُرسل المستند {einv.source_document.number} للإبلاغ. لا يُسلَّم "
                   "للمشتري ولا يُرحَّل حتى يعتمد المستشار الضريبي ذلك.", einvoice=einv)
        elif outcome == O.REJECTED:
            _transition(einv, Status.REJECTED)
        elif outcome == O.TRANSIENT_ERROR:
            einv.transient_failures += 1
            limit = int(_setting("ZATCA_MAX_ATTEMPTS_STANDARD", 10))
            if einv.zatca_category == EInvoice.Category.STANDARD and einv.transient_failures >= limit:
                _transition(einv, Status.FAILED_FINAL)
                _alert(einv.entity, _zatca_admins(einv.entity), "failed_final",
                       f"{einv.source_document.number} could not be cleared after {limit} attempts; it is not issued.",
                       f"تعذر اعتماد المستند {einv.source_document.number} بعد {limit} محاولات؛ لم يصدر.", einvoice=einv)
            else:  # simplified: never failed_final for transient errors; keep retrying at the cap (R-24)
                _transition(einv, Status.FAILED_RETRYABLE)
                einv.next_attempt_at = now + _backoff(einv.transient_failures)
        else:
            _transition(einv, Status.FAILED_FINAL)
            _alert(einv.entity, _zatca_admins(einv.entity), "failed_final",
                   f"ZATCA refused {einv.source_document.number} (HTTP {response.status}); it needs attention.",
                   f"رفضت الهيئة المستند {einv.source_document.number} (HTTP {response.status})؛ يحتاج إلى متابعة.",
                   einvoice=einv)
        _save(einv)
    if outcome == SubmissionAttempt.Outcome.CLEARANCE_DISABLED:  # R-44: the same signed XML to reporting, now
        return _post(einv, user, now, EInvoice.Api.REPORTING)
    return einv


# --- results back to invoicing (§5, R-22, R-23) --------------------------------------------------------------

def _qr_for_invoicing(einv):
    if einv.zatca_category == EInvoice.Category.SIMPLIFIED:
        return einv.qr_tlv
    if einv.cleared_xml:  # standard: the QR in ZATCA's cleared XML (R-15)
        return ubl.qr_from_xml(einv.cleared_xml.encode("utf-8"))
    return ""


def _invoicing_status(einv):
    if einv.status in CLEARED | REPORTED:
        return str(einv.status)
    if einv.status == Status.REJECTED:
        return "rejected"
    return "pending"  # incl. reported_clearance_disabled and failed_final: not issued, not posted (R-44, OQ-7)


def _sync_key(einv):
    return f"{einv.status}:{einv.hold_reason}:{einv.messages.count()}"


def _sync_invoicing(einv):
    """Hand the current result to ``invoicing.services.on_zatca_result`` (as the ZATCA queue user); a failure
    (e.g. the posting) is retried by the next worker run."""
    from invoicing import services as invoicing_services
    from invoicing.models import SalesDocument
    from invoicing.zatca_port import ZatcaResult

    einv = EInvoice.objects.select_related("source_document").get(pk=einv.pk)
    key = _sync_key(einv)
    if einv.invoicing_synced == key or einv.source_document.status != SalesDocument.Status.ISSUED:
        return
    messages = tuple({"code": m.code, "text_en": m.message_en, "text_ar": m.display_ar}
                     for m in einv.messages.order_by("id"))
    result = ZatcaResult(status=_invoicing_status(einv), icv=einv.icv, invoice_hash=einv.invoice_hash,
                         previous_hash=einv.pih, qr=_qr_for_invoicing(einv), messages=messages)
    try:
        invoicing_services.on_zatca_result(einv.source_document, result, user=_queue_user())
    except Exception as exc:  # noqa: BLE001 - kept for the next run; the zatca side is already committed
        log.warning("zatca: invoicing callback for e-invoice %s failed: %s", einv.pk, type(exc).__name__)
        return
    einv.invoicing_synced = key
    _save(einv)


def is_issued(document):
    """R-22 / R-23 (port ``is_issued``): standard documents only once cleared; simplified ones once stamped and
    not rejected. ``reported_clearance_disabled`` and ``failed_*`` standard documents are not issued (OQ-7)."""
    from invoicing import zatca_port

    einv = EInvoice.objects.filter(source_document_id=document.pk).first()
    if einv is None:
        return zatca_port.issued_by_status(document)
    if einv.zatca_category == EInvoice.Category.STANDARD:
        return einv.status in CLEARED
    return einv.icv is not None and einv.status != Status.REJECTED


# --- queue worker and scheduled checks (R-23, R-24, R-27, R-35) ------------------------------------------------

def _blocked_by_earlier(einv):
    """``ZATCA_SERIALISE_PER_EGS`` (default off, R-27): wait for earlier ICVs of the same unit."""
    if not _setting("ZATCA_SERIALISE_PER_EGS", False):
        return False
    return EInvoice.objects.filter(egs_unit=einv.egs_unit, icv__lt=einv.icv, status__in=(
        Status.STAMPED, Status.QUEUED, Status.SUBMITTING, Status.FAILED_RETRYABLE)).exists()


def process_queue(*, user, now=None):
    """One worker run (ZATCA queue system user): stamp pending documents, requeue due retries, submit queued
    documents (not held back by earlier ICVs unless serialised, R-27), resend results to invoicing, and run
    the deadline checks. Returns counts."""
    now = now or _now()
    counts = {"stamped": 0, "sent": 0}
    for entity in access.entities_for(user, PROCESS):
        access.require(user, entity, PROCESS)
        due = Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now)
        for einv in EInvoice.objects.filter(due, entity=entity, status=Status.PENDING,
                                            hold_reason__in=RETRIED_HOLDS).order_by("created_at", "pk"):
            if _stamp(einv, user, now).status == Status.QUEUED:
                counts["stamped"] += 1
        for einv in EInvoice.objects.filter(due, entity=entity, status=Status.FAILED_RETRYABLE).order_by("icv"):
            with transaction.atomic():
                einv = EInvoice.objects.select_for_update().get(pk=einv.pk)
                if einv.status == Status.FAILED_RETRYABLE:
                    _transition(einv, Status.QUEUED)
                    _save(einv)
        for einv in EInvoice.objects.filter(entity=entity, status=Status.QUEUED).order_by("egs_unit", "icv"):
            if _blocked_by_earlier(einv):
                continue
            _send(einv, user, now)
            counts["sent"] += 1
        for einv in EInvoice.objects.filter(entity=entity, source_document__status="issued").exclude(
                status=Status.PENDING, hold_reason=""):
            _sync_invoicing(einv)
        _deadline_checks(entity, now)
    return counts


def run_deadline_checks(*, user, now=None):
    """R-23 / R-35 scheduled job (also run by :func:`process_queue`)."""
    now = now or _now()
    for entity in access.entities_for(user, PROCESS):
        access.require(user, entity, PROCESS)
        _deadline_checks(entity, now)


LATE_TITLE = ("Report the late simplified invoice {number} to ZATCA", "إبلاغ الهيئة عن تأخر الإبلاغ عن الفاتورة المبسطة {number}")
LATE_INSTRUCTIONS = (
    "The 24-hour reporting deadline of {number} passed ({deadline:%Y-%m-%d %H:%M} UTC). 1) File the failure notice on "
    "ZATCA's website ({url}) and record its reference here. 2) Once the document is reported, notify ZATCA that the "
    "incident ended and record that reference. Retries continue meanwhile.",
    "انقضت مهلة الإبلاغ البالغة 24 ساعة للمستند {number} ({deadline:%Y-%m-%d %H:%M} بالتوقيت العالمي). 1) قدّم إشعار "
    "التعطل في موقع الهيئة ({url}) وسجّل مرجعه هنا. 2) بعد الإبلاغ عن المستند، أبلغ الهيئة بانتهاء العطل وسجّل المرجع. "
    "تستمر المحاولات في الأثناء.",
)


def _deadline_checks(entity, now):
    alert_before = timedelta(hours=float(_setting("ZATCA_DEADLINE_ALERT_HOURS", 2)))
    open_docs = EInvoice.objects.filter(entity=entity, zatca_category=EInvoice.Category.SIMPLIFIED,
                                        icv__isnull=False).exclude(status__in=(*REPORTED, Status.REJECTED))
    for einv in open_docs.filter(reporting_deadline__lte=now + alert_before, deadline_alerted_at__isnull=True):
        with transaction.atomic():
            einv = EInvoice.objects.select_for_update().get(pk=einv.pk)
            if einv.deadline_alerted_at:
                continue
            einv.deadline_alerted_at = now
            _save(einv)
            number, deadline = einv.source_document.number, f"{einv.reporting_deadline:%Y-%m-%d %H:%M}"
            _alert(entity, _zatca_admins(entity), "reporting_deadline",
                   f"Simplified invoice {number} is not reported yet; the deadline is {deadline} UTC.",
                   f"لم يُبلَّغ عن الفاتورة المبسطة {number} بعد؛ المهلة تنتهي في {deadline} بالتوقيت العالمي.",
                   einvoice=einv)
    queue = None
    for einv in open_docs.filter(reporting_deadline__lte=now).exclude(tasks__kind=ComplianceTask.Kind.LATE_REPORTING_NOTICE):
        queue = queue or _queue_user()
        number = einv.source_document.number
        fmt = dict(number=number, deadline=einv.reporting_deadline, url=_setting(
            "ZATCA_FAILURE_NOTICE_URL",
            "https://zatca.gov.sa/en/E-Invoicing/FailureNotifications/Pages/VerifyTaxpayer.aspx"))
        with transaction.atomic():
            _save(ComplianceTask(entity=entity, einvoice=einv, kind=ComplianceTask.Kind.LATE_REPORTING_NOTICE,
                                 title_en=LATE_TITLE[0].format(**fmt), title_ar=LATE_TITLE[1].format(**fmt),
                                 instructions_en=LATE_INSTRUCTIONS[0].format(**fmt),
                                 instructions_ar=LATE_INSTRUCTIONS[1].format(**fmt), created_by=queue,
                                 source=Source.SYSTEM))
            _alert(entity, _zatca_admins(entity), "late_reporting_notice", LATE_TITLE[0].format(**fmt),
                   LATE_TITLE[1].format(**fmt), einvoice=einv)


def mark_task_notified(task, *, user, notified_at, notice_reference, evidence=None):
    """R-35 step 1: open -> notified, only with ``notified_at`` and ``notice_reference``."""
    task = ComplianceTask.objects.get(pk=task.pk)
    access.require(user, task.entity, "zatca.close_late_notice")
    if task.status != ComplianceTask.Status.OPEN:
        raise ZatcaError("Only an open task moves to notified.", "تنتقل المهمة المفتوحة فقط إلى حالة الإبلاغ.",
                         code="task_status")
    if not notified_at or not (notice_reference or "").strip():
        raise ZatcaError("The failure notice needs its date and the reference ZATCA gave (R-35).",
                         "يحتاج إشعار التعطل إلى تاريخه والمرجع الذي أعطته الهيئة.", code="task_fields")
    task.status, task.notified_at, task.notice_reference = ComplianceTask.Status.NOTIFIED, notified_at, notice_reference
    if evidence is not None:
        task.evidence = evidence
    return _save(task)


def mark_task_done(task, *, user, recovered_notified_at, recovery_notice_reference):
    """R-35 step 2: notified -> done, only after the document is reported and with the recovery notice."""
    task = ComplianceTask.objects.select_related("einvoice").get(pk=task.pk)
    access.require(user, task.entity, "zatca.close_late_notice")
    if task.status != ComplianceTask.Status.NOTIFIED:
        raise ZatcaError("Only a notified task is closed.", "تُغلق المهمة المبلَّغ عنها فقط.", code="task_status")
    if task.einvoice.status not in REPORTED:
        raise ZatcaError("The document is not reported yet; the incident has not ended.",
                         "لم يُبلَّغ عن المستند بعد؛ لم ينته العطل.", code="task_not_reported")
    if not recovered_notified_at or not (recovery_notice_reference or "").strip():
        raise ZatcaError("Closing needs the date and reference of the notice that the incident ended (R-35).",
                         "يحتاج الإغلاق إلى تاريخ ومرجع إشعار انتهاء العطل.", code="task_fields")
    task.status, task.done_by = ComplianceTask.Status.DONE, user
    task.recovered_notified_at, task.recovery_notice_reference = recovered_notified_at, recovery_notice_reference
    return _save(task)


def retry_now(einvoice, *, user):
    """§7 manual retry (zatca_admin or accountant): a ``failed_retryable`` document is queued at once; a document
    waiting for its EGS unit or the secret store is tried at the next run. ``failed_final`` is not retried here:
    R-20 has no transition out of it (awaits a human decision, OQ-7)."""
    einv = EInvoice.objects.select_related("entity").get(pk=einvoice.pk)
    access.require(user, einv.entity, "zatca.retry_failed")
    with transaction.atomic():
        einv = EInvoice.objects.select_for_update().get(pk=einv.pk)
        if einv.status == Status.FAILED_RETRYABLE:
            _transition(einv, Status.QUEUED)
            einv.next_attempt_at = None
        elif einv.status == Status.PENDING and einv.hold_reason in RETRIED_HOLDS:
            einv.next_attempt_at = None
        else:
            raise ZatcaError(f"A {einv.status} document is not retried (R-20).",
                             f"لا تُعاد محاولة مستند في حالة {einv.status}.", code="not_retryable")
        return _save(einv)


# --- archive (R-30, R-38) and lists (R-3) ------------------------------------------------------------------

def export_archive(einvoice, *, user):
    """``(file name, XML bytes)``: the cleared XML when there is one, else the signed XML (R-30, AT-59). Documents
    of individual buyers also need ``invoicing.view_personal_data`` (access OQ-7 default)."""
    einv = EInvoice.objects.select_related("entity", "source_document__customer").get(pk=einvoice.pk)
    access.require(user, einv.entity, "zatca.export_archive")
    if einv.source_document.customer.customer_kind == "individual":
        access.require(user, einv.entity, "invoicing.view_personal_data")
    if not einv.signed_xml:
        raise ZatcaError("Nothing is archived before stamping.", "لا يُؤرشف شيء قبل الختم.", code="not_stamped")
    xml = (einv.cleared_xml or einv.signed_xml).encode("utf-8")
    issue = einv.issue_datetime.astimezone(dt_timezone.utc)
    vat = einv.source_document.seller_vat_number
    return rules.archive_file_name(vat, issue.strftime("%Y-%m-%d"), issue.strftime("%H:%M:%S"),
                                   einv.source_document.number), xml


def purge_archive(entity, *, user, today=None):
    """R-30 / AT-34 / OQ-8: no purge job is enabled. Reports what is kept; deletes nothing."""
    access.require(user, entity, "zatca.manage_profile")
    today = today or _now().date()
    kept = EInvoice.objects.filter(entity=entity).count()
    expired = EInvoice.objects.filter(entity=entity, retention_until__lt=today).count()
    return {"kept": kept, "deleted": 0, "past_retention": expired}


def einvoices(user, entity=None):
    """E-invoices the user may view (``zatca.view``), entity-filtered (R-3)."""
    qs = access.scope(EInvoice.objects.select_related("entity", "source_document"), user, "zatca.view")
    return qs.filter(entity=entity) if entity is not None else qs


def einvoice_statuses(user, entity=None):
    """Statuses only (``zatca.view_status``, the one zatca right an agent may hold, R-34)."""
    qs = access.scope(EInvoice.objects.all(), user, "zatca.view_status")
    if entity is not None:
        qs = qs.filter(entity=entity)
    return qs.values("pk", "entity_id", "source_document_id", "status")
