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
