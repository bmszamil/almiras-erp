"""Shared argument handling for the migration commands (migration.md R16): a named human user, an entity, dry run
unless ``--apply``, bilingual errors."""
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError

from core.access import AccessDenied
from core.models import Entity
from migration import readers


def add_common(parser, *, apply=True):
    parser.add_argument("--entity", required=True, help="Entity code / رمز الكيان")
    parser.add_argument("--user", required=True, help="Username of the person doing this / اسم المستخدم")
    parser.add_argument("--source-system", default="falcon", help="Legacy system (default falcon)")
    if apply:
        parser.add_argument("--apply", action="store_true", help="Write (default: dry run) / تنفيذ فعلي")


def resolve(options):
    entity = Entity.objects.filter(code=options["entity"]).first()
    if entity is None:
        raise CommandError(f"Unknown entity {options['entity']}. / الكيان {options['entity']} غير معروف.")
    user = get_user_model().objects.filter(username=options["user"], is_active=True).first()
    if user is None:
        raise CommandError(f"Unknown or inactive user {options['user']}. / المستخدم {options['user']} غير معروف أو غير نشط.")
    return entity, user


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"{value!r} is not a date (YYYY-MM-DD). / {value!r} ليس تاريخًا.") from None


def read_tb(options):
    if not options.get("tb"):
        return None
    return readers.read_trial_balance(options["tb"], options.get("tb_columns"))


def read_journal(options):
    if not options.get("journal"):
        return None
    return readers.read_falcon_journal(options["journal"])


def run(fn):
    """Call ``fn``; turn refusals into a CommandError with the bilingual text."""
    try:
        return fn()
    except AccessDenied as exc:
        raise CommandError(str(exc)) from None
    except readers.ReaderError as exc:
        raise CommandError(str(exc)) from None
    except ValidationError as exc:
        raise CommandError("\n".join(exc.messages)) from None
