"""PostgreSQL only: the immutability triggers (migration 0002) and AT-12 chain continuity under concurrency."""
import threading
from datetime import timedelta

import pytest
from django.db import DatabaseError, connection, connections, transaction

from zatca import services
from zatca.conftest import NOW, einvoice_of, issue, onboard, run
from zatca.models import EgsCredential, EgsUnit, EInvoice

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL triggers and row locks")


def _refused(sql, params=()):
    with pytest.raises(DatabaseError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


def _ok(sql, params=()):
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql, params)


@pytest.fixture
def cleared(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    doc = issue(zw, zw.tst1)
    run(zw)
    zw.einv = einvoice_of(doc)
    assert zw.einv.status == "cleared"
    return zw


@pytest.mark.django_db
def test_triggers_keep_attempts_messages_events_and_alerts_append_only(cleared):
    from zatca.models import ValidationMessage, ZatcaAlert

    e = cleared.einv
    ValidationMessage.objects.create(entity=e.entity, einvoice=e, level="info", origin="local_rules", code="x",
                                     message_en="x")
    ZatcaAlert.objects.create(entity=e.entity, recipient=cleared.admin1, kind="x", message_en="x", message_ar="x")
    _refused("UPDATE zatca_submissionattempt SET http_status = 500 WHERE einvoice_id = %s", [e.pk])
    _refused("DELETE FROM zatca_submissionattempt WHERE einvoice_id = %s", [e.pk])
    _refused("UPDATE zatca_onboardingevent SET detail = '{}'")
    _refused("DELETE FROM zatca_onboardingevent")
    _refused("DELETE FROM zatca_validationmessage")
    _refused("UPDATE zatca_zatcaalert SET message_en = 'x'")


@pytest.mark.django_db
def test_triggers_keep_signed_and_cleared_xml(cleared):
    e = cleared.einv
    for sql in ("UPDATE zatca_einvoice SET signed_xml = '<x/>' WHERE id = %s",
                "UPDATE zatca_einvoice SET cleared_xml = '<y/>' WHERE id = %s",
                "UPDATE zatca_einvoice SET invoice_hash = 'x' WHERE id = %s",
                "UPDATE zatca_einvoice SET icv = 99 WHERE id = %s",
                "UPDATE zatca_einvoice SET uuid = gen_random_uuid() WHERE id = %s",
                "UPDATE zatca_einvoice SET status = 'pending' WHERE id = %s",
                "UPDATE zatca_einvoice SET retention_until = retention_until - 1 WHERE id = %s",
                "DELETE FROM zatca_einvoice WHERE id = %s"):
        _refused(sql, [e.pk])
    _ok("UPDATE zatca_einvoice SET invoicing_synced = invoicing_synced WHERE id = %s", [e.pk])


@pytest.mark.django_db
def test_triggers_guard_the_pih_chain(cleared):
    egs = cleared.egs1.pk
    _refused("UPDATE zatca_egsunit SET last_icv = last_icv + 1 WHERE id = %s", [egs])  # no such e-invoice
    _refused("UPDATE zatca_egsunit SET last_icv = last_icv + 2 WHERE id = %s", [egs])
    _refused("UPDATE zatca_egsunit SET last_invoice_hash = 'x' WHERE id = %s", [egs])
    _refused("UPDATE zatca_egsunit SET last_icv = 0, last_invoice_hash = %s WHERE id = %s",
             ["NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ==", egs])
    _refused("DELETE FROM zatca_egsunit WHERE id = %s", [egs])
    _refused("INSERT INTO zatca_egsunit (entity_id, code, name_en, name_ar, branch_name_en, branch_name_ar, "
             "serial_number, location, industry, supports_standard, supports_simplified, status, last_icv, "
             "last_invoice_hash, private_key_ref, csr_pem, created_at, created_by_id, source) SELECT entity_id, 'X', "
             "'x', 'x', '', '', 's', 'l', 'i', true, false, 'draft', 5, last_invoice_hash, '', '', now(), "
             "created_by_id, 'human' FROM zatca_egsunit WHERE id = %s", [egs])
    # a pending document cannot take an ICV out of order or outside stamping
    doc = issue(cleared, cleared.tst1)
    pending = einvoice_of(doc)
    _refused("UPDATE zatca_einvoice SET icv = 7, status = 'stamped', signed_xml = '<x/>', invoice_hash = 'h', "
             "pih = 'p', egs_unit_id = %s WHERE id = %s", [egs, pending.pk])
    _refused("UPDATE zatca_einvoice SET status = 'queued' WHERE id = %s", [pending.pk])
    _refused("INSERT INTO zatca_einvoice SELECT * FROM zatca_einvoice WHERE id = %s", [cleared.einv.pk])


@pytest.mark.django_db
def test_triggers_on_credentials_delegations_tasks_and_keys(cleared):
    cred = EgsCredential.objects.get(egs_unit=cleared.egs1, kind="production", status="active")
    _refused("UPDATE zatca_egscredential SET certificate_pem = 'x' WHERE id = %s", [cred.pk])
    _refused("UPDATE zatca_egscredential SET status = 'active' WHERE status = 'superseded'")
    _refused("DELETE FROM zatca_egscredential WHERE id = %s", [cred.pk])
    _refused("UPDATE zatca_keycustodiandelegation SET scope = 'master_key'")
    _refused("DELETE FROM zatca_keycustodiandelegation")
    _refused("UPDATE zatca_entitymasterkey SET key_version = 0")
    _refused("DELETE FROM zatca_entitymasterkey")
    _refused("DELETE FROM zatca_zatcaprofile")


@pytest.mark.django_db
def test_trigger_task_transitions(zw):
    zw.egs1 = onboard(zw, zw.tst1, zw.admin1)
    zw.fakes.client.script["reporting"] = [zw.fakes.client.timeout()] * 3
    doc = issue(zw, zw.tst1, simplified=True)
    run(zw)
    einv = einvoice_of(doc)
    services.run_deadline_checks(user=zw.queue, now=einv.reporting_deadline + timedelta(minutes=1))
    task = einv.tasks.get()
    _refused("UPDATE zatca_compliancetask SET status = 'done' WHERE id = %s", [task.pk])
    _refused("UPDATE zatca_compliancetask SET status = 'notified' WHERE id = %s", [task.pk])  # no reference
    _refused("DELETE FROM zatca_compliancetask WHERE id = %s", [task.pk])


@pytest.mark.django_db(transaction=True)
def test_at12_concurrent_stamps_get_consecutive_icvs_and_an_intact_chain():
    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed
    from zatca.conftest import build_world
    from zatca.testing import fake_zatca_services

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply_all(apps)
    with fake_zatca_services() as (store, client):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        client.cert_valid_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        zw = build_world(SimpleNamespace(store=store, client=client))
        egs = onboard(zw, zw.tst1, zw.admin1)
        first, second = einvoice_of(issue(zw, zw.tst1)), einvoice_of(issue(zw, zw.tst1, simplified=True))
        result = {}

        def other():
            try:
                result["einv"] = services.stamp_document(second, user=zw.queue, now=NOW)
            except Exception as exc:  # noqa: BLE001 - reported in the assertion
                result["error"] = repr(exc)
            finally:
                connections.close_all()

        with transaction.atomic():
            services.stamp_document(first, user=zw.queue, now=NOW)  # holds the EGS row lock until commit
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(timeout=1.5)
            assert thread.is_alive(), "the second stamp must wait for the EGS row lock (R-11)"
        thread.join(timeout=20)
    assert "error" not in result, result
    a, b = EInvoice.objects.get(pk=first.pk), EInvoice.objects.get(pk=second.pk)
    assert (a.icv, b.icv) == (1, 2)
    assert a.pih == services.crypto.INITIAL_PIH and b.pih == a.invoice_hash
    unit = EgsUnit.objects.get(pk=egs.pk)
    assert unit.last_icv == 2 and unit.last_invoice_hash == b.invoice_hash
