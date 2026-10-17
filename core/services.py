"""Entity services: issue readiness (invoicing.md R69) and setup data (D10)."""
import json
import re
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Entity

SETUP_DATA_DIR = Path(__file__).resolve().parent / "setup_data"
ALMIRAS_SETUP_DATA = SETUP_DATA_DIR / "almiras.json"
# The only entity setup_almiras may create or change. The file never chooses it.
ALMIRAS_CODE = "ALM"

# Seller fields that must be filled before any document can be issued (R69).
ISSUE_REQUIRED_FIELDS = (
    "legal_name_en",
    "legal_name_ar",
    "legal_form",
    "cr_number",
    "building_no",
    "street_en",
    "street_ar",
    "district_en",
    "district_ar",
    "postal_code",
    "city_en",
    "city_ar",
    "country",
)
# Optional to issue, but checked when filled because they are copied to documents.
# additional_no: optional since 2026-10-16 (§3.0, verification Z3). unified_number: R72.
OPTIONAL_CHECKED_FIELDS = ("additional_no", "unified_number")
# Required once the entity has a VAT number ("Y if vat_number", §3.0).
VAT_REQUIRED_FIELDS = ("vat_effective_date", "vat_return_period")

# Fields that setup data may set. Anything else in the file is rejected.
SETUP_FIELDS = frozenset(
    ISSUE_REQUIRED_FIELDS
    + VAT_REQUIRED_FIELDS
    + OPTIONAL_CHECKED_FIELDS
    + ("vat_number", "short_address")
)


def _field_problem(entity, name):
    """True if ``name`` on ``entity`` is blank (whitespace counts as blank) or fails its validators.

    Values saved with ``objects.create()`` or ``update()`` skip ``full_clean``,
    so the field validators are run again here (AT-48).
    """
    value = getattr(entity, name)
    if value is None or (isinstance(value, str) and not value.strip()):
        return True
    field = Entity._meta.get_field(name)
    try:
        field.run_validators(value)
        if field.choices and value not in {k for k, _ in field.flatchoices}:
            raise ValidationError("invalid choice", code="invalid_choice")
    except ValidationError:
        return True
    return False


def entity_ready_to_issue(entity, standard_rated=False):
    """Return the names of seller fields that are missing or invalid (R69).

    An empty list means the entity may issue. ``standard_rated`` is True when
    the document has standard-rated (S) lines, which need a VAT number (R11).
    Each field is checked with its model validators (format rules in §3.0),
    so a bad value written without ``full_clean`` is reported, not issued.
    """
    missing = [f for f in ISSUE_REQUIRED_FIELDS if _field_problem(entity, f)]
    missing += [f for f in OPTIONAL_CHECKED_FIELDS if getattr(entity, f) and _field_problem(entity, f)]
    # Any stored VAT number, even whitespace, is checked: it would be copied to documents.
    if entity.vat_number or standard_rated:
        if _field_problem(entity, "vat_number"):
            missing.append("vat_number")
        missing += [f for f in VAT_REQUIRED_FIELDS if _field_problem(entity, f)]
    return missing


def load_setup_data(path, expected_code=ALMIRAS_CODE):
    """Read an entity setup-data file (JSON) and return its dict.

    The file's ``code`` must equal ``expected_code``: the command decides which
    entity is changed, the file only confirms it. A file written for another
    entity is rejected before anything is read from the database.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not data.get("code") or not isinstance(data.get("fields"), dict):
        raise ValidationError(
            "Setup data needs 'code' and a 'fields' object. / يجب أن تحتوي بيانات الإعداد على 'code' وكائن 'fields'."
        )
    if data["code"] != expected_code:
        raise ValidationError(
            f"Setup data is for entity {data['code']!r}; this command only sets up {expected_code!r}. / "
            f"بيانات الإعداد للكيان {data['code']!r}؛ هذا الأمر يُعِدّ الكيان {expected_code!r} فقط.",
            code="wrong_entity",
        )
    unknown = set(data["fields"]) - SETUP_FIELDS
    if unknown:
        raise ValidationError(
            f"Setup data has unknown entity fields: {', '.join(sorted(unknown))} / "
            "تحتوي بيانات الإعداد على حقول غير معروفة."
        )
    return data


_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _parse_date(name, value):
    """Parse a setup-data date written as YYYY-MM-DD; anything else is a bilingual ValidationError.

    ``date.fromisoformat`` alone also accepts forms such as ``20250101`` or
    ``2025-W01-1``; setup data must use the one documented form.
    """
    if value is None or isinstance(value, date):
        return value
    try:
        if not isinstance(value, str) or not _ISO_DATE.match(value):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError(
            {name: f"{value!r} is not a valid ISO date (YYYY-MM-DD). / {value!r} ليس تاريخًا صالحًا بصيغة YYYY-MM-DD."},
            code="invalid_date",
        ) from None


@transaction.atomic
def apply_entity_setup(entity, fields):
    """Set registration and address fields on ``entity``, validate and save.

    Returns the names of the fields whose value changed (empty: nothing saved).
    Idempotent: applying the same fields twice leaves the entity unchanged.
    Only ``entity`` is touched; no other entity is read or written.
    """
    changed = []
    for name, value in fields.items():
        if name not in SETUP_FIELDS:
            raise ValidationError(
                f"Field {name!r} cannot be set from setup data. / لا يمكن تعيين الحقل {name!r} من بيانات الإعداد."
            )
        if name == "vat_effective_date":
            value = _parse_date(name, value)
        if getattr(entity, name) != value:
            changed.append(name)
        setattr(entity, name, value)
    entity.full_clean()
    if changed or entity.pk is None:
        entity.save()
    return changed


# --- sensitive entity settings (access.md R32, A13, OQ-18) ----------------------------

# Changed only through change_entity_settings (base_currency: ledger.services.set_functional_currency).
SENSITIVE_ENTITY_FIELDS = ("sharia_enforced", "consolidation", "parent", "is_active")
ENTITY_SETTINGS_PERMISSION = "core.change_entity_settings"
# R48 (A17): turning Sharia enforcement off is a two-step four-eyes action on the group parent:
# managing_partner requests (first approval), chairman approves (second), managing_partner applies.
SHARIA_OFF_PERMISSION = "core.disable_sharia_enforcement"


def _reason_required(reason):
    if not reason or not str(reason).strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")


def sharia_off_payload(entity, reason):
    """The change a Sharia switch-off request approves (R48): exactly this entity, off, for this reason."""
    return {"entity": entity.code, "sharia_enforced": False, "reason": str(reason)}


def new_entity_sharia_off_payload(code, parent, reason):
    """The creation a Sharia-off request approves (R48): a new entity ``code`` under ``parent``, Sharia off."""
    return {"create": True, "entity": code, "parent": parent.code, "sharia_enforced": False, "reason": str(reason)}


def request_sharia_enforcement_off(entity, *, user, reason):
    """Step 1 of R48: a managing partner of the group asks to turn Sharia enforcement off for ``entity``.

    Opening the request is the managing partner's approval. It is recorded on the group parent (target = the
    entity) and waits for the group's chairman (:func:`decide_sharia_enforcement_off`); then a managing partner
    applies it with ``change_entity_settings(entity, sharia_enforced=False, dual_control_request=...)``.
    """
    from core.access import services as access

    _reason_required(reason)
    entity = Entity.objects.get(pk=entity.pk)
    if not entity.sharia_enforced:
        raise ValidationError(f"{entity.code} does not enforce Sharia. / الكيان {entity.code} لا يطبق الضوابط الشرعية.")
    return access.request_dual_control(user, access.group_parent_of(entity), SHARIA_OFF_PERMISSION, entity,
                                       sharia_off_payload(entity, reason))


def request_new_entity_sharia_off(parent, code, *, user, reason):
    """Step 1 of R48 for ``create_entity`` with ``sharia_enforced=False``: target = the parent entity."""
    from core.access import services as access

    _reason_required(reason)
    parent = Entity.objects.get(pk=parent.pk)
    return access.request_dual_control(user, access.group_parent_of(parent), SHARIA_OFF_PERMISSION, parent,
                                       new_entity_sharia_off_payload(code, parent, reason))


def decide_sharia_enforcement_off(request, *, user, approve, note=""):
    """Step 2 of R48: the group's chairman approves or rejects the request (never its requester)."""
    from core.access import services as access

    if request.permission != SHARIA_OFF_PERMISSION:
        raise ValidationError("Not a Sharia switch-off request. / ليس طلب إيقاف للضوابط الشرعية.")
    return access.decide_dual_control(user, request, approve, note)


def _setting_value(entity, name):
    if name == "parent":
        return entity.parent.code if entity.parent_id else None
    return getattr(entity, name)


def change_entity_settings(entity, *, user, reason, dual_control_request=None, **changes):
    """Change ``sharia_enforced``, ``consolidation``, ``parent`` or ``is_active`` of ``entity`` (R32).

    ``user`` needs ``core.change_entity_settings`` on the group parent of the
    entity (OQ-18 default: managing_partner only); moving the entity to
    another group needs it on that group too. Writes one
    ``entity_settings_changed`` AccessLog row with before and after values.

    Turning Sharia enforcement **off** (R48) also needs ``dual_control_request``: a request opened by a
    managing partner (:func:`request_sharia_enforcement_off`) and approved by the group's chairman, for exactly
    this entity and ``reason``; it is used once, and the change must be alone in the call. Turning it **on**
    stays a single step by the managing partner (R49).
    """
    from core.access import services as access

    unknown = set(changes) - set(SENSITIVE_ENTITY_FIELDS)
    if "base_currency" in unknown:
        raise ValidationError(
            "The functional currency is changed with ledger.services.set_functional_currency. / "
            "تُغيَّر العملة الوظيفية من خلال خدمة تغيير العملة الوظيفية."
        )
    if unknown:
        raise ValidationError(f"Not an entity setting: {', '.join(sorted(unknown))}. / ليس من إعدادات الكيان.")
    _reason_required(reason)
    entity = Entity.objects.get(pk=entity.pk)
    group = access.group_parent_of(entity)
    _require_settings_right(access, user, group, changes)
    switching_off = "sharia_enforced" in changes and not changes["sharia_enforced"] and entity.sharia_enforced
    if switching_off and set(changes) != {"sharia_enforced"}:
        raise ValidationError(
            "Turning Sharia enforcement off is a change of its own; change the other settings separately (R48). / "
            "إيقاف تطبيق الضوابط الشرعية تغيير مستقل؛ غيّر الإعدادات الأخرى بشكل منفصل.", code="sharia_off_alone",
        )
    if dual_control_request is not None and not switching_off:
        raise ValidationError(
            "A four-eyes request is used only to turn Sharia enforcement off. / "
            "يُستخدم طلب الرقابة المزدوجة لإيقاف تطبيق الضوابط الشرعية فقط.", code="dual_control_not_needed",
        )
    if switching_off:
        if dual_control_request is None:  # R48: MP request, chairman approval, then apply
            access.deny(user, group, SHARIA_OFF_PERMISSION, "dual_control")
        with transaction.atomic():  # the request is used only if the change is written
            access.require_dual_control(user, group, SHARIA_OFF_PERMISSION, dual_control_request,
                                        sharia_off_payload(entity, reason), target=entity)
            return _change_entity_settings(entity, user, reason, changes, dual_control_request=dual_control_request)
    new_parent = changes.get("parent", entity.parent)
    if new_parent is None and changes.get("is_active", entity.is_active) is False:
        # Nobody could reactivate it: every right is checked on the group parent (access.md R6, R32).
        raise ValidationError(
            "The top-level group parent entity cannot be deactivated. / لا يمكن إيقاف الكيان الأم الأعلى للمجموعة."
        )
    if "parent" in changes and new_parent is not None:
        node = new_parent
        while node is not None:  # no cycles: the new parent is not the entity or below it
            if node.pk == entity.pk:
                raise ValidationError("An entity cannot be placed under itself. / لا يمكن وضع الكيان تحت نفسه.")
            node = node.parent
        _require_settings_right(access, user, access.group_parent_of(new_parent), changes)
    return _change_entity_settings(entity, user, reason, changes)


def _require_settings_right(access, user, group, changes):
    """``core.change_entity_settings`` on ``group``; on an inactive group parent only reactivation (catalogue I).

    ``reactivation=True`` lets the I permission pass on the inactive group
    parent; the check below then refuses anything but ``is_active=True``.
    """
    grant = access.require(user, group, ENTITY_SETTINGS_PERMISSION, reactivation=True)
    if not group.is_active and changes != {"is_active": True}:
        access.deny(user, group, ENTITY_SETTINGS_PERMISSION, "inactive_entity_reactivate_only")
    return grant


@transaction.atomic
def reactivate_entity_by_operator(entity, reason, os_user):
    """Recovery (R32, R39): set ``is_active`` back on an inactive entity from the ``reactivate_entity`` command.

    Only where nobody can do it through :func:`change_entity_settings`: the
    entity is inactive and is either a top-level entity (no parent) or in a
    group whose group parent is inactive (every right on the group is checked
    on that parent). An inactive subsidiary of an active group is reactivated
    by the group's managing partner instead. Logged as an
    ``entity_settings_changed`` row with the operator's OS user and no actor.
    """
    from core.access.models import AccessLog
    from core.access.services import group_parent_of, log

    if not reason or not str(reason).strip():
        raise ValidationError("A reason is required. / السبب مطلوب.")
    entity = Entity.objects.select_for_update().get(pk=entity.pk)
    if entity.is_active:
        raise ValidationError(f"{entity.code} is already active. / الكيان {entity.code} نشط بالفعل.")
    group = group_parent_of(entity)
    if entity.parent_id is not None and group.is_active:
        raise ValidationError(
            f"{entity.code} is in the active group {group.code}: reactivate it with change_entity_settings "
            f"(core.change_entity_settings on {group.code}). / الكيان {entity.code} ضمن المجموعة النشطة {group.code}: "
            "أعد تفعيله من خلال خدمة إعدادات الكيان."
        )
    entity.is_active = True
    entity.save(update_fields=["is_active"])
    log(
        AccessLog.Event.ENTITY_SETTINGS_CHANGED, entity=entity, permission=ENTITY_SETTINGS_PERMISSION, source="command",
        details={"before": {"is_active": False}, "after": {"is_active": True}, "reason": reason, "os_user": os_user,
                 "command": "reactivate_entity"},
    )
    return entity


@transaction.atomic
def _change_entity_settings(entity, user, reason, changes, dual_control_request=None):
    from core.access.models import AccessLog
    from core.access.services import log

    entity = Entity.objects.select_for_update().get(pk=entity.pk)
    before = {name: _setting_value(entity, name) for name in changes}
    for name, value in changes.items():
        setattr(entity, name, value)
    after = {name: _setting_value(entity, name) for name in changes}
    changed = [name for name in changes if before[name] != after[name]]
    if not changed:
        return entity
    entity.clean_fields(exclude=[f.name for f in Entity._meta.fields if f.name not in changed and f.attname not in changed])
    entity.clean()
    entity.save(update_fields=changed)
    log(
        AccessLog.Event.ENTITY_SETTINGS_CHANGED, actor=user, entity=entity, permission=ENTITY_SETTINGS_PERMISSION,
        details={
            "before": {k: before[k] for k in changed}, "after": {k: after[k] for k in changed}, "reason": reason,
            **({"dual_control_request": dual_control_request.pk} if dual_control_request is not None else {}),
        },
    )
    return entity


def create_entity(entity, *, user, reason="Created in admin / أُنشئ من لوحة الإدارة", dual_control_request=None):
    """Create ``entity`` (an unsaved Entity) as a member of an existing group (access.md R32, A13).

    ``user`` needs ``core.change_entity_settings`` on the group parent of the
    chosen parent entity; that group parent must be active. A new top-level
    group (no parent) is set up by an operator, not here. Writes one
    ``entity_settings_changed`` row with the new entity's sensitive settings.

    An entity created with ``sharia_enforced=False`` needs an approved R48 request
    (:func:`request_new_entity_sharia_off`) naming its code, its parent and ``reason``.
    """
    from core.access import services as access

    if entity.pk is not None:
        raise ValidationError("This entity already exists. / هذا الكيان موجود بالفعل.")
    if entity.parent_id is None:
        raise ValidationError(
            "A new entity is created under a parent entity of an existing group. / يُنشأ الكيان الجديد تحت كيان أم لمجموعة قائمة."
        )
    group = access.group_parent_of(Entity.objects.get(pk=entity.parent_id))
    access.require(user, group, ENTITY_SETTINGS_PERMISSION)
    if not group.is_active:
        access.deny(user, group, ENTITY_SETTINGS_PERMISSION, "inactive_entity_reactivate_only")
    if entity.sharia_enforced:
        if dual_control_request is not None:
            raise ValidationError(
                "A four-eyes request is used only to create an entity without Sharia enforcement. / "
                "يُستخدم طلب الرقابة المزدوجة فقط لإنشاء كيان لا يطبق الضوابط الشرعية.", code="dual_control_not_needed",
            )
        return _create_entity(entity, user, reason)
    if dual_control_request is None:  # R48: created with Sharia off only after MP request and chairman approval
        access.deny(user, group, SHARIA_OFF_PERMISSION, "dual_control")
    parent = Entity.objects.get(pk=entity.parent_id)
    with transaction.atomic():
        access.require_dual_control(user, group, SHARIA_OFF_PERMISSION, dual_control_request,
                                    new_entity_sharia_off_payload(entity.code, parent, reason), target=parent)
        return _create_entity(entity, user, reason, dual_control_request=dual_control_request)


@transaction.atomic
def _create_entity(entity, user, reason, dual_control_request=None):
    from core.access.models import AccessLog
    from core.access.services import log

    entity.full_clean()
    entity.save()
    log(
        AccessLog.Event.ENTITY_SETTINGS_CHANGED, actor=user, entity=entity, permission=ENTITY_SETTINGS_PERMISSION,
        details={"created": True, "before": None, "reason": reason, "after": {
            **{name: _setting_value(entity, name) for name in SENSITIVE_ENTITY_FIELDS},
            "base_currency": entity.base_currency_id,
        }, **({"dual_control_request": dual_control_request.pk} if dual_control_request is not None else {})},
    )
    return entity
