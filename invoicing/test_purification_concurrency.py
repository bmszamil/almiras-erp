"""R54 under real concurrency (PostgreSQL only): two purification transfers posted at the same moment cannot both
pass the limit (review finding 1). Made-up amounts."""
import threading
from datetime import date

import pytest
from django.db import connection, connections, transaction

from invoicing import services
from invoicing.models import InvoicingError, PurificationTransfer
from invoicing.test_purification import flagged_invoice

pytestmark = pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL-only locking test")


@pytest.fixture
def seeded(transactional_db):
    """Earlier transactional tests flush the database: put back the seeded rows the ``w`` fixture relies on."""
    from django.apps import apps
    from django.contrib.auth import get_user_model

    from core.access.models import Role, RolePermission, UserProfile
    from core.access.seed import apply_seed, apply_system_users
    from core.currency_seed import apply_currency_seed
    from core.models import Currency
    from invoicing import reference_seed

    apply_seed(Role, RolePermission)
    apply_currency_seed(Currency)
    apply_system_users(get_user_model(), UserProfile)
    reference_seed.apply_all(apps)


@pytest.mark.django_db(transaction=True)
def test_r54_concurrent_transfers_serialise_on_the_entity(seeded, w):
    flagged_invoice(w, "1000.00")
    first, second = (services.create_purification_transfer(w.alm, user=w.accountant, kind="transfer",
                                                            date=date(2027, 1, 31), amount="600.00")
                     for _ in range(2))
    result = {}

    def other():
        try:
            services.post_purification_transfer(second, user=w.cfo)
            result["posted"] = True
        except InvoicingError as exc:
            result["code"] = exc.code
        except Exception as exc:  # noqa: BLE001 - reported in the assertion
            result["error"] = repr(exc)
        finally:
            connections.close_all()

    with transaction.atomic():
        services.post_purification_transfer(first, user=w.cfo)  # holds the entity lock until commit
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(timeout=1.5)
        assert thread.is_alive(), "the second post should wait for the entity lock"
    thread.join(timeout=10)
    assert result == {"code": "purification_invalid"}
    assert list(PurificationTransfer.objects.filter(status="posted").values_list("pk", flat=True)) == [first.pk]
