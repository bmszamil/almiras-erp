"""R7: cost pools hold only expense accounts of their own entity, never Sharia-prohibited ones.

Checked on every way accounts are added to a pool (service, admin, API or shell)."""
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from .models import CostPool


@receiver(m2m_changed, sender=CostPool.accounts.through)
def check_pool_accounts(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action != "pre_add" or not pk_set:
        return
    from .services import validate_pool_accounts

    if reverse:  # adding pools to an account
        for pool in CostPool.objects.filter(pk__in=pk_set):
            validate_pool_accounts(pool, [instance])
    else:
        validate_pool_accounts(instance, list(model.objects.filter(pk__in=pk_set)))
