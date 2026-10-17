"""Seed refresh for databases migrated before review finding 11 and nit 12 (fresh databases already get these rows
from 0002): the Umm al-Qura Eid dates in ``calendar.official_holidays``, ``leave.notice_absence`` (Art. 78),
``gosi.legal_age`` and the empty ``gosi.exclusion_reason_map`` (R65, OQ-42).

Frozen data (written 2026-10-17); the live seed module is not imported, so this migration does the same thing
whenever it runs. Rows are created unverified; an existing row is refreshed only while it is a pristine loader row
(unverified, unused, never verified, entered by nobody: nit 21). Nothing is ever verified here (R7)."""
from django.db import migrations

ROWS = [('SA',
  'calendar.official_holidays',
  '2000-01-01',
  {'date_ranges': [],
   'eid_al_adha': {'1440': ['2019-08-10', '2019-08-13'],
                   '1441': ['2020-07-30', '2020-08-02'],
                   '1442': ['2021-07-19', '2021-07-22'],
                   '1443': ['2022-07-08', '2022-07-11'],
                   '1444': ['2023-06-27', '2023-06-30'],
                   '1445': ['2024-06-15', '2024-06-18'],
                   '1446': ['2025-06-05', '2025-06-08'],
                   '1447': ['2026-05-26', '2026-05-29'],
                   '1448': ['2027-05-15', '2027-05-18'],
                   '1449': ['2028-05-04', '2028-05-07'],
                   '1450': ['2029-04-23', '2029-04-26'],
                   '1451': ['2030-04-12', '2030-04-15'],
                   '1452': ['2031-04-01', '2031-04-04'],
                   '1453': ['2032-03-21', '2032-03-24'],
                   '1454': ['2033-03-10', '2033-03-13'],
                   '1455': ['2034-02-28', '2034-03-03'],
                   '1456': ['2035-02-17', '2035-02-20'],
                   '1457': ['2036-02-06', '2036-02-09'],
                   '1458': ['2037-01-25', '2037-01-28'],
                   '1459': ['2038-01-15', '2038-01-18'],
                   '1460': ['2039-01-04', '2039-01-07'],
                   '1461': ['2039-12-25', '2039-12-28'],
                   '1462': ['2040-12-13', '2040-12-16']},
   'eid_al_fitr': {'1440': ['2019-06-04', '2019-06-07'],
                   '1441': ['2020-05-23', '2020-05-26'],
                   '1442': ['2021-05-12', '2021-05-15'],
                   '1443': ['2022-05-01', '2022-05-04'],
                   '1444': ['2023-04-21', '2023-04-24'],
                   '1445': ['2024-04-09', '2024-04-12'],
                   '1446': ['2025-03-30', '2025-04-02'],
                   '1447': ['2026-03-19', '2026-03-22'],
                   '1448': ['2027-03-09', '2027-03-12'],
                   '1449': ['2028-02-26', '2028-02-29'],
                   '1450': ['2029-02-14', '2029-02-17'],
                   '1451': ['2030-02-03', '2030-02-06'],
                   '1452': ['2031-01-24', '2031-01-27'],
                   '1453': ['2032-01-13', '2032-01-16'],
                   '1454': ['2033-01-02', '2033-01-05'],
                   '1455': ['2033-12-22', '2033-12-25'],
                   '1456': ['2034-12-11', '2034-12-14'],
                   '1457': ['2035-11-30', '2035-12-03'],
                   '1458': ['2036-11-18', '2036-11-21'],
                   '1459': ['2037-11-08', '2037-11-11'],
                   '1460': ['2038-10-29', '2038-11-01'],
                   '1461': ['2039-10-18', '2039-10-21'],
                   '1462': ['2040-10-06', '2040-10-09']},
   'fixed_month_days': ['02-22', '09-23']},
  'https://www.hrsd.gov.sa/en/policies/implementing-regulations-labor-law',
  'Regs Art. 19, 24 (Founding Day, National Day; Eids 1440-1462 AH from the Umm al-Qura table via hijridate, check '
  'against announcements) | Checked 2026-10-17 against the HRSD text '
  '(docs/compliance/hr-payroll-labour-law-2026-10-17.md); MP verification pending (R7)'),
 ('SA',
  'leave.notice_absence',
  '2000-01-01',
  {'days_per_week': '1', 'hours_per_week': '8'},
  'https://www.hrsd.gov.sa/en/policies/labor-law',
  'Labour Law Art. 78 | Checked 2026-10-17 against the HRSD text '
  '(docs/compliance/hr-payroll-labour-law-2026-10-17.md); MP verification pending (R7)'),
 ('SA',
  'gosi.legal_age',
  '2024-07-03',
  {'legacy_clause_fifth': {'article': 'SIL Royal Decree Clause Fifth(1), (2)', 'schedule': []},
   'legacy_m33': {'article': 'SIL M/33 Art. 38(1)(a)', 'years': '60'},
   'new_m273': {'article': 'SIL Art. 16(1)', 'years': '65'}},
  'https://cmsgosi.gosi.gov.sa/SystemsAndRegulations/Documents/SocialInsuranceSystem1421.pdf',
  'SIL Art. 16(1); SIL M/33 Art. 38(1)(a), GOSI booklet 2025 p.21; Royal Decree Clause Fifth (schedule not loaded) | '
  'read 2026-10-17; MP verification pending (R7)'),
 ('SA',
  'gosi.exclusion_reason_map',
  '2000-01-01',
  {},
  'https://cmsgosi.gosi.gov.sa/SystemsAndRegulations/RequiredDocuments/Documents/form7.pdf',
  "GOSI Form 7 item 1 (GOSI guide, secondary); violations fines rules Aug 2024 item 2 [empty until GOSI's reason "
  'list is supplied, hr OQ-42] | read 2026-10-17; MP verification pending (R7)')]


def forward(apps, schema_editor):
    Parameter = apps.get_model("hr", "StatutoryParameter")
    for jurisdiction, code, valid_from, value, url, ref in ROWS:
        rows = Parameter.objects.filter(jurisdiction=jurisdiction, code=code, valid_from=valid_from)
        current = rows.exclude(status="superseded").first()
        if current is None:
            if not rows.exists():
                Parameter.objects.create(jurisdiction=jurisdiction, code=code, valid_from=valid_from, value=value,
                                         source_url=url, source_reference=ref, status="unverified")
            continue
        pristine = (current.status == "unverified" and current.used_at is None and current.verified_by_id is None
                    and current.verified_at is None and current.entered_by_id is None)
        if pristine and (current.value, current.source_url, current.source_reference) != (value, url, ref):
            current.value, current.source_url, current.source_reference = value, url, ref
            current.save(update_fields=["value", "source_url", "source_reference"])


class Migration(migrations.Migration):
    dependencies = [("hr", "0016_record_guard_triggers")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
