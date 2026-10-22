"""migration.md R7, R8, R12, R13 (account maps; AT-5, AT-6, AT-13)."""
import pytest

from core.access import AccessDenied
from core.access.testing import make_user, member
from ledger.models import Account
from migration import services
from migration import testing as t
from migration.conftest import map_rows
from migration.models import AccountMap, AccountMapRow, MigrationError


def test_at5_map_is_drafted_then_approved_by_a_second_person(w):
    amap = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    assert amap.status == "draft" and amap.version == 1 and amap.rows.count() == 9
    with pytest.raises(AccessDenied):  # the accountant holds no approval right
        t.approve_map(amap, approver=w.a)
    both = make_user("mig.both")
    member(both, w.alm, "accountant")
    member(both, w.alm, "approver")
    services.set_map_row(amap, user=both, legacy_code="1101", erp_code="1110", legacy_name="الصندوق")
    with pytest.raises(AccessDenied) as exc:  # last modifier (R13, access R14)
        t.approve_map(amap, approver=both)
    assert exc.value.code == "self_approval"
    approved = t.approve_map(amap, approver=w.p)
    assert (approved.status, approved.approved_by) == ("approved", w.p)


def test_approved_map_is_locked_everywhere(w, mapped):
    with pytest.raises(MigrationError):
        services.set_map_row(mapped, user=w.a, legacy_code="1101", erp_code="1110")
    row = mapped.rows.first()
    row.note = "changed"
    with pytest.raises(MigrationError):
        row.save()
    with pytest.raises(MigrationError):
        AccountMapRow.objects.filter(account_map=mapped).update(note="x")
    with pytest.raises(MigrationError):
        AccountMapRow.objects.filter(account_map=mapped).delete()
    with pytest.raises(MigrationError):  # a bulk update cannot unlock it either
        AccountMap.objects.filter(pk=mapped.pk).update(status="draft")
    with pytest.raises(MigrationError):
        AccountMap.objects.filter(pk=mapped.pk).delete()
    mapped.note = "x"
    with pytest.raises(MigrationError):
        mapped.save()
    with pytest.raises(MigrationError):
        mapped.delete()


def test_new_version_supersedes_the_approved_one(w, mapped):
    v2 = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    assert v2.version == 2
    with pytest.raises(MigrationError):  # one draft at a time
        services.create_account_map(w.alm, user=w.a, rows=map_rows())
    t.approve_map(v2, approver=w.p)
    assert AccountMap.objects.get(pk=mapped.pk).status == "superseded"
    assert services._approved_map(w.alm, "falcon").pk == v2.pk


@pytest.mark.parametrize("legacy_code, erp_code, counterparty, code", [
    ("4101", "1100", None, "class_mismatch"),      # income code to an asset
    ("1101", "4100", None, "class_mismatch"),
    ("1101", "1000", None, "not_postable"),        # header
    ("1101", "3400", None, "not_postable"),        # report-only (and the wrong class)
    ("2601", "3400", None, "not_postable"),
    ("1301", "1210", None, "counterparty_missing"),  # intercompany without counterparty (rule 6)
    ("1301", "1210", "ALM", "counterparty_self"),
    ("1101", "1100", "SUB", "counterparty_not_intercompany"),
    ("11", "1100", None, "bad_code"),
    ("1101", "9999", None, "unknown_account"),
])
def test_at6_rows_that_do_not_map_cleanly_are_refused(w, legacy_code, erp_code, counterparty, code):
    amap = services.create_account_map(w.alm, user=w.a)
    with pytest.raises(MigrationError) as exc:
        services.set_map_row(amap, user=w.a, legacy_code=legacy_code, erp_code=erp_code, counterparty=counterparty)
    assert exc.value.code == code
    assert any("ا" in m for m in exc.value.messages)  # Arabic text present


def test_rows_never_point_to_another_entitys_account(w):
    amap = services.create_account_map(w.alm, user=w.a)
    sub_cash = Account.objects.get(entity=w.sub, code="1100")
    with pytest.raises(MigrationError) as exc:
        services.set_map_row(amap, user=w.a, legacy_code="1101", erp_code=sub_cash)
    assert exc.value.code == "other_entity"


def test_intercompany_row_with_counterparty_and_csv_import(w, tmp_path):
    path = tmp_path / "alm-account-map.csv"
    path.write_bytes(t.table_csv(["legacy_code", "legacy_name", "erp_code", "counterparty", "note"],
                                 [["1301", "مستحق من شركة تابعة", "1210", "SUB", "تابعة واحدة"],
                                  ["1101", "الصندوق", "1100", "", ""]]))
    amap = services.import_account_map(w.alm, path, user=w.a)
    row = amap.rows.get(legacy_code="1301")
    assert (row.account.code, row.counterparty, row.note) == ("1210", w.sub, "تابعة واحدة")


def test_bad_csv_rows_are_all_reported_and_nothing_is_kept(w):
    rows = [{"legacy_code": "1101", "erp_code": "1100", "row": 2}, {"legacy_code": "4101", "erp_code": "1100", "row": 3},
            {"legacy_code": "1101", "erp_code": "1110", "row": 4}]
    with pytest.raises(MigrationError) as exc:
        services.create_account_map(w.alm, user=w.a, rows=rows)
    text = "\n".join(exc.value.messages)
    assert "Row 3" in text and "البند 3" in text and "Row 4" in text
    assert not AccountMap.objects.filter(entity=w.alm).exists()


def test_at13_other_entitys_accountant_cannot_map(w):
    with pytest.raises(AccessDenied):
        services.create_account_map(w.alm, user=w.s, rows=map_rows())
    assert list(services.account_maps(w.s)) == []


def test_viewer_sees_maps_but_cannot_draft(w, mapped):
    assert list(services.account_maps(w.v, w.alm)) == [mapped]
    with pytest.raises(AccessDenied):
        services.create_account_map(w.alm, user=w.v)


def test_draft_can_be_discarded_but_not_an_approved_map(w, mapped):
    draft = services.create_account_map(w.alm, user=w.a, rows=map_rows())
    services.discard_account_map(draft, user=w.a)
    assert not AccountMap.objects.filter(pk=draft.pk).exists()
    with pytest.raises(MigrationError):
        services.discard_account_map(mapped, user=w.a)
