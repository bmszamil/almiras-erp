"""``manage.py migration_post --entity ALM --user approver --kind opening|journal [--entries N ...] [--apply]``
(migration R16).

Posts import drafts by a person other than the preparer: each posting is the decision on the draft's approval
request in the approval engine (activity ``ledger.manual_entry``), and the ledger posts it through
``ledger.services.post_entry`` with the engine's grant. A draft changed since the import, or an opening entry whose
sub-ledgers do not tie out (R22), is refused. All or nothing. Under a multi-step matrix an earlier step is recorded
and the draft waits for the next approver. Dry run unless ``--apply``.
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
            for key in result["pending"]:
                for p in result["details"][key]:
                    self.stdout.write(f"  [waiting] {p}")
        else:
            self.stdout.write("dry run: nothing posted / تجربة: لم يُرحَّل شيء")
