"""Read-only admin for payroll (business logic lives in payroll.services; every change goes through it)."""
from django.contrib import admin

from . import models


class ReadOnly(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for m in (models.PayrollSettings, models.PayrollRun, models.PayrollInput, models.EmployeeLoan, models.PayrollPayment,
          models.WpsFile, models.MudadSubmission, models.GosiReconciliation, models.MaternityGosiCase):
    admin.site.register(m, ReadOnly)
