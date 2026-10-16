"""Entity services: issue readiness (invoicing.md R69) and setup data (D10)."""
import json
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Entity, validate_vat_number

SETUP_DATA_DIR = Path(__file__).resolve().parent / "setup_data"
ALMIRAS_SETUP_DATA = SETUP_DATA_DIR / "almiras.json"
# The only entity setup_almiras may create or change. The file never chooses it.
ALMIRAS_CODE = "ALM"

# Seller fields that must be filled before any document can be issued (R69).
ISSUE_REQUIRED_FIELDS = (
    "legal_name_en",
    "legal_name_ar",
    "cr_number",
    "building_no",
    "street_en",
    "street_ar",
    "district_en",
    "district_ar",
    "additional_no",
    "postal_code",
    "city_en",
    "city_ar",
    "country",
)
# Required once the entity has a VAT number ("Y if vat_number", §3.0).
VAT_REQUIRED_FIELDS = ("vat_effective_date", "vat_return_period")

# Fields that setup data may set. Anything else in the file is rejected.
SETUP_FIELDS = frozenset(
    ISSUE_REQUIRED_FIELDS
    + VAT_REQUIRED_FIELDS
    + ("legal_form", "unified_number", "vat_number", "short_address")
)


def entity_ready_to_issue(entity, standard_rated=False):
    """Return the names of seller fields that are missing or invalid (R69).

    An empty list means the entity may issue. ``standard_rated`` is True when
    the document has standard-rated (S) lines, which need a VAT number (R11).
    """
    missing = [f for f in ISSUE_REQUIRED_FIELDS if not getattr(entity, f)]
    if entity.vat_number:
        try:
            validate_vat_number(entity.vat_number)
        except ValidationError:
            missing.append("vat_number")
    elif standard_rated:
        missing.append("vat_number")
    if entity.vat_number or standard_rated:
        missing += [f for f in VAT_REQUIRED_FIELDS if not getattr(entity, f)]
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


def _parse_date(name, value):
    try:
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
        if name == "vat_effective_date" and isinstance(value, str):
            value = _parse_date(name, value)
        if getattr(entity, name) != value:
            changed.append(name)
        setattr(entity, name, value)
    entity.full_clean()
    if changed or entity.pk is None:
        entity.save()
    return changed
