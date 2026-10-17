"""HR operator jobs that are not user actions (management commands only). They live outside ``hr/services.py``
because they act for no user (the access scan, core/test_access_scan.py, requires a rights check in every public
service function); each command is run by an operator from the KSA-hosted host (H7)."""
from django.db import IntegrityError, transaction

from . import crypto, services
from .models import Employee, EmployeeDocument, PregnancyNotice, error


def resignation_deadlines(*, today=None):
    """``hr_resignation_deadlines`` (daily, Art. 79 bis): day-20 and day-27 reminders to the approvers, and deemed
    acceptance on day 30 (or at the end of a postponement). See ``hr.services._resignation_deadlines``."""
    return services._resignation_deadlines(today or services._today())


def gosi_exclusion_reminders(*, today=None):
    """``hr_gosi_exclusion_reminders`` (daily, R65): reminders on the 25th and the last day of the leaving month
    while a GOSI exclusion item is open. See ``hr.services._gosi_exclusion_reminders``."""
    return services._gosi_exclusion_reminders(today or services._today())


def rehash_personal_data():
    """``hr_rehash_personal_data`` (finding 8): after a key rotation, or once ``HR_PERSONAL_DATA_HASH_KEY`` is set,
    re-hash every ID number under the current hash secret and re-encrypt every personal-data value under the
    current first key, so old keys can be retired. Idempotent. Returns ``{"employees": n, "documents": m}``. Two
    current employees of one entity that turn out to share an ID number stop the run (nothing is changed)."""
    encrypted = [f.name for f in Employee._meta.concrete_fields if isinstance(f, crypto.EncryptedTextField)]
    counts = {"employees": 0, "documents": 0}
    try:
        with transaction.atomic():
            for emp in Employee._base_manager.order_by("pk").iterator():
                values = {name: getattr(emp, name) for name in encrypted}
                Employee._base_manager.filter(pk=emp.pk).update(id_number_hash=crypto.keyed_hash(emp.id_number),
                                                                **values)
                counts["employees"] += 1
            for doc in EmployeeDocument._base_manager.order_by("pk").iterator():
                EmployeeDocument._base_manager.filter(pk=doc.pk).update(number=doc.number)
                counts["documents"] += 1
            for notice in PregnancyNotice._base_manager.order_by("pk").iterator():
                PregnancyNotice._base_manager.filter(pk=notice.pk).update(document_ref=notice.document_ref)
                counts["documents"] += 1
    except IntegrityError:
        raise error("Two current employees of one entity have the same ID number; nothing was re-hashed (R9).",
                    "لدى موظفَين حاليين في كيان واحد رقم الهوية نفسه؛ لم يُعَد حساب أي تجزئة.", code="id_duplicate") from None
    return counts

