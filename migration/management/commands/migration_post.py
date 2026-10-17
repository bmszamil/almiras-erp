"""``manage.py migration_post --entity ALM --user approver --kind opening|journal [--entries N ...] [--apply]``
(migration R16).

Posts import drafts through ``ledger.services.post_entry``, one ``ledger.post_entry`` grant per draft, by a person
other than the preparer. A draft changed since the import is refused. All or nothing. Dry run unless ``--apply``.
"""
from django.core.management.base import BaseCommand, CommandError

from migration import services

from ._common import add_common, resolve, run


class Command(BaseCommand):
    help = "Post imported legacy drafts (second person; dry run unless --apply). / ترحيل القيود المستوردة."

    def add_arguments(self, parser):
        add_common(parser)
        parser.add_argument("--kind", choices=["opening", "journal"], required=True)
        parser.add_argument("--entries", nargs="*", help="Legacy entry numbers (default: all drafts of the kind)")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        result = run(lambda: services.post_imported_entries(entity, user=user, kind=o["kind"], legacy_keys=o["entries"],
                                                            apply=o["apply"], source_system=o["source_system"]))
        for key in result["problems"]:
            for p in result["details"][key]:
                self.stdout.write(f"  [refused] {p}")
        self.stdout.write(f"ready / جاهز: {len(result['ready'])}; refused / مرفوض: {len(result['problems'])}; "
                          f"already posted / مرحّل سابقًا: {len(result['already_posted'])}")
        if result["problems"]:
            raise CommandError("Some drafts cannot be posted; nothing was posted. / تعذّر ترحيل بعض المسودات؛ لم يُرحَّل شيء.")
        if o["apply"]:
            for entry in result["posted"]:
                self.stdout.write(f"  posted / رُحّل {entry.number} {entry.reference}")
        else:
            self.stdout.write("dry run: nothing posted / تجربة: لم يُرحَّل شيء")
