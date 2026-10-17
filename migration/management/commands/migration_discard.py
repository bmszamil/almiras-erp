"""``manage.py migration_discard --entity ALM --user u --kind opening|journal [--entries N ...] [--apply]``
(migration R17). Deletes import drafts and their links so they can be imported again; posted entries are kept (they
are reversed in the ledger). Dry run unless ``--apply``."""
from django.core.management.base import BaseCommand

from migration import services

from ._common import add_common, resolve, run


class Command(BaseCommand):
    help = "Discard imported legacy drafts (dry run unless --apply). / حذف مسودات القيود المستوردة."

    def add_arguments(self, parser):
        add_common(parser)
        parser.add_argument("--kind", choices=["opening", "journal"], required=True)
        parser.add_argument("--entries", nargs="*")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        result = run(lambda: services.discard_imported_drafts(entity, user=user, kind=o["kind"],
                                                              legacy_keys=o["entries"], apply=o["apply"],
                                                              source_system=o["source_system"]))
        mode = "discarded / حُذفت" if o["apply"] else "would discard (dry run) / سيُحذف (تجربة)"
        self.stdout.write(f"{mode}: {len(result['drafts'])} draft(s); kept (posted) / مرحّلة: {len(result['posted'])}")
