"""Entity services: issue readiness (invoicing.md R69) and setup data (D10)."""
import json
from datetime import date
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import Entity, validate_vat_number

SETUP_DATA_DIR = Path(__file__).resolve().parent / "setup_data"
ALMIRAS_SETUP_DATA = SETUP_DATA_DIR / "almiras.json"

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


def load_setup_data(path):
    """Read an entity setup-data file (JSON) and return its dict."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not data.get("code") or not isinstance(data.get("fields"), dict):
        raise ValidationError("Setup data needs 'code' and a 'fields' object.")
    unknown = set(data["fields"]) - SETUP_FIELDS
    if unknown:
        raise ValidationError(f"Setup data has unknown entity fields: {', '.join(sorted(unknown))}")
    return data


@transaction.atomic
def apply_entity_setup(entity, fields):
    """Set registration and address fields on ``entity``, validate and save.

    Idempotent: applying the same fields twice leaves the entity unchanged.
    Only ``entity`` is touched; no other entity is read or written.
    """
    for name, value in fields.items():
        if name not in SETUP_FIELDS:
            raise ValidationError(f"Field {name!r} cannot be set from setup data.")
        if name == "vat_effective_date" and isinstance(value, str):
            value = date.fromisoformat(value)
        setattr(entity, name, value)
    entity.full_clean()
    entity.save()
    return entity
