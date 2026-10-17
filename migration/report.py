"""Text and CSV forms of a validation report (migration.md R9, R11). Presentation only; no rules here.

Reports may name legacy accounts and amounts: write them outside git (``exports/`` is ignored) unless they are
reviewed and hold no personal data (migration-agent rules).
"""
import csv
from pathlib import Path


def _fmt(value):
    return "" if value is None else str(value)


def render(report, *, limit=200):
    """Lines of text for the console: summary, problems, unmapped codes, totals and tie-out differences."""
    out = [f"{report.kind} {report.file_name or '-'} sha256={report.sha256[:12] or '-'} entity={report.entity} "
           f"map=v{report.map_version or '-'} ({report.map_status or '-'})"]
    for key, value in report.summary.items():
        out.append(f"  {key}: {value}")
    out.append(f"errors / أخطاء: {len(report.errors)}; warnings / تنبيهات: {len(report.warnings)}")
    for p in report.problems[:limit]:
        where = " ".join(x for x in (f"row={p.row}" if p.row else "", f"entry={p.entry_no}" if p.entry_no else "",
                                     f"code={p.account_code}" if p.account_code else "") if x)
        out.append(f"  [{p.severity}] {p.code} {where} {p.message}".rstrip())
    if len(report.problems) > limit:
        out.append(f"  … {len(report.problems) - limit} more (see the CSV report) / المزيد في ملف التقرير")
    if report.unmapped:
        out.append("unmapped codes / أكواد غير مربوطة:")
        for u in report.unmapped:
            out.append(f"  {u['code']} {u['name']} rows={u['rows']} debit={u['debit']} credit={u['credit']}")
    if report.already_imported:
        out.append(f"already imported / مستورد سابقًا: {len(report.already_imported)}")
    diffs = [t for t in report.tie_out if t["difference"] or (t["tb_movement"] is not None and t["tb_movement"] != t["journal"])]
    if report.tie_out:
        out.append(f"tie-out / المطابقة: {len(report.tie_out)} accounts, {len(diffs)} with differences")
    return out


def rows(report):
    """Every part of the report as flat dicts (one CSV)."""
    for p in report.problems:
        yield {"section": "problem", **p.as_dict()}
    for t in report.account_totals:
        yield {"section": "legacy_total", "account_code": t["code"], "name": t["name"], "rows": t["rows"],
               "debit": t["debit"], "credit": t["credit"], "net": t["net"], "erp_code": t["erp_code"]}
    for t in report.erp_totals:
        yield {"section": "erp_total", "erp_code": t["erp_code"], "debit": t["debit"], "credit": t["credit"],
               "net": t["net"], "legacy_codes": t["legacy_codes"]}
    for t in report.tie_out:
        yield {"section": "tie_out", "account_code": t["code"], "name": t["name"], "opening": t["opening"],
               "journal": t["journal"], "expected_closing": t["expected_closing"], "closing": t["closing"],
               "difference": t["difference"], "tb_movement": _fmt(t["tb_movement"])}


FIELDS = ["section", "severity", "code", "row", "entry_no", "account_code", "name", "erp_code", "legacy_codes", "rows",
          "debit", "credit", "net", "opening", "journal", "expected_closing", "closing", "difference", "tb_movement",
          "message"]


def write_csv(report, path):
    """The report as UTF-8 CSV with BOM (opens with Arabic intact in spreadsheet programs)."""
    path = Path(path)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows(report):
            writer.writerow({k: _fmt(v) for k, v in row.items()})
    return path
