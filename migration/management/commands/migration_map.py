"""``manage.py migration_map --entity ALM --user u [--import map.csv | --workbook wb.xlsx | --submit VERSION |
--approve VERSION | --return VERSION] [--apply]`` (migration R12, R13, W13).

Without an action it lists the entity's maps. ``--import`` drafts the next map version from a
``docs/migration/<entity>-account-map.csv`` file, ``--workbook`` from the entity's rows on the Account mapping sheet of
a filled migration workbook; ``--submit`` (the preparer) submits a draft for approval, which opens its request in the
approval engine; ``--approve`` / ``--return`` is the approver's decision on it through the engine (R13 as amended,
M7: never the creator, submitter or last modifier). Dry run unless ``--apply``: the same checks run and nothing is
kept.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from migration import services, workbook
from migration.models import AccountMap

from ._common import add_common, read_book, resolve, run


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Draft, approve or list legacy account maps (dry run unless --apply). / خرائط حسابات النظام السابق."

    def add_arguments(self, parser):
        add_common(parser)
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--import", dest="import_file", help="Account map CSV to draft from")
        group.add_argument("--workbook", help="Filled migration workbook: draft from its Account mapping sheet")
        group.add_argument("--submit", type=int, help="Version number of the draft to submit for approval")
        group.add_argument("--approve", type=int, help="Version number of the submitted draft to approve (engine)")
        group.add_argument("--return", dest="return_", type=int, help="Version number of the draft to return (engine)")
        parser.add_argument("--note", default="")

    def handle(self, *args, **o):
        entity, user = resolve(o)
        system = o["source_system"]
        version = next((o[k] for k in ("submit", "approve", "return_") if o.get(k) is not None), None)
        if not o["import_file"] and not o["workbook"] and version is None:
            for m in run(lambda: services.account_maps(user, entity).filter(source_system=system)):
                self.stdout.write(f"v{m.version} {m.status} rows={m.rows.count()} created_by={m.created_by} "
                                  f"submitted_by={m.submitted_by or '-'} approved_by={m.approved_by or '-'}")
            return

        def action():
            if o["import_file"]:
                amap = services.import_account_map(entity, o["import_file"], user=user, source_system=system,
                                                   note=o["note"])
                return f"draft v{amap.version}: {amap.rows.count()} rows / مسودة"
            if o["workbook"]:
                rows = workbook.account_map_rows_for(read_book(o), entity.code)
                if not rows:
                    raise CommandError(f"No mapping rows for {entity.code} in the workbook. / "
                                       f"لا توجد صفوف ربط للكيان {entity.code} في الملف.")
                amap = services.create_account_map(entity, user=user, rows=rows, source_system=system, note=o["note"])
                return f"draft v{amap.version}: {amap.rows.count()} rows / مسودة"
            amap = AccountMap.objects.filter(entity=entity, source_system=system, version=version).first()
            if amap is None:
                raise CommandError(f"No map version {version}. / لا توجد النسخة {version}.")
            if o.get("submit") is not None:
                amap = services.submit_account_map(amap, user=user)
                return f"submitted v{amap.version} for approval / قُدّمت للاعتماد"
            if o.get("return_") is not None:
                amap = services.decide_account_map(amap, user=user, approve=False, note=o["note"])
                return f"returned v{amap.version} / أُعيدت"
            amap = services.decide_account_map(amap, user=user)
            if amap.status != AccountMap.Status.APPROVED:
                return f"approval step recorded for v{amap.version}; the next approver decides / سُجّل الاعتماد"
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
