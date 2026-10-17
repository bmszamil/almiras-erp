"""``manage.py migration_map --entity ALM --user u [--import map.csv | --approve VERSION] [--apply]`` (migration R12, R13).

Without an action it lists the entity's maps. ``--import`` drafts the next map version from a
``docs/migration/<entity>-account-map.csv`` file; ``--approve`` approves a draft (a person other than its creator and
last modifier). Dry run unless ``--apply``: the same checks run and nothing is kept.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from migration import services
from migration.models import AccountMap

from ._common import add_common, resolve, run


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Draft, approve or list legacy account maps (dry run unless --apply). / خرائط حسابات النظام السابق."

    def add_arguments(self, parser):
        add_common(parser)
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--import", dest="import_file", help="Account map CSV to draft from")
        group.add_argument("--approve", type=int, help="Version number of the draft to approve")
        parser.add_argument("--note", default="")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        system = o["source_system"]
        if not o["import_file"] and o["approve"] is None:
            for m in run(lambda: services.account_maps(user, entity).filter(source_system=system)):
                self.stdout.write(f"v{m.version} {m.status} rows={m.rows.count()} created_by={m.created_by} "
                                  f"approved_by={m.approved_by or '-'}")
            return

        def action():
            if o["import_file"]:
                amap = services.import_account_map(entity, o["import_file"], user=user, source_system=system,
                                                   note=o["note"])
                return f"draft v{amap.version}: {amap.rows.count()} rows / مسودة"
            amap = AccountMap.objects.filter(entity=entity, source_system=system, version=o["approve"]).first()
            if amap is None:
                raise CommandError(f"No map version {o['approve']}. / لا توجد النسخة {o['approve']}.")
            amap = services.approve_account_map(amap, user=user)
            return f"approved v{amap.version} / تم الاعتماد"

        message = None
        try:
            with transaction.atomic():
                message = run(action)
                if not o["apply"]:
                    raise _DryRun
        except _DryRun:
            self.stdout.write(f"dry run, nothing kept / تجربة دون حفظ: {message}")
            return
        self.stdout.write(message)
