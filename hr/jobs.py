"""HR operator jobs that are not user actions (management commands only). They live outside ``hr/services.py``
because they act for no user (the access scan, core/test_access_scan.py, requires a rights check in every public
service function); each command is run by an operator from the KSA-hosted host (H7)."""
from django.db import IntegrityError, transaction

from . import crypto
from .models import Employee, EmployeeDocument, error


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
    except IntegrityError:
        raise error("Two current employees of one entity have the same ID number; nothing was re-hashed (R9).",
                    "لدى موظفَين حاليين في كيان واحد رقم الهوية نفسه؛ لم يُعَد حساب أي تجزئة.", code="id_duplicate") from None
    return counts

