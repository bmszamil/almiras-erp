"""Thin, entity-scoped, read-only admin for HR (access R2, R24; hr R2, R10).

Every change goes through ``hr.services``. Lists are scoped to the entities where the user holds ``hr.view``.
Personal data (names, ID number, birth date, gender, contact, bank, residence; R10) is never a form field here:
the detail page shows it through ``hr.services.employee_record``, which masks it without
``hr.view_personal_data`` in that entity and logs every unmasked view (``personal_data_viewed``). Object labels are
the employee code only (R11).
"""
from django.contrib import admin
from django.utils.html import format_html_join

from core.admin import EntityScopedAdmin

from . import services
from .models import (
    Assignment,
    AssignmentEvent,
    Department,
    Employee,
    EmployeeDocument,
    EmploymentContract,
    EosbCalculation,
    FinalSettlementInput,
    Grade,
    HrAlert,
    HrSettings,
    LeaveLedger,
    LeaveRequest,
    LeaveType,
    SalaryComponent,
    StatutoryParameter,
    Termination,
)


class ReadOnlyHrAdmin(EntityScopedAdmin):
    access_view_permission = "hr.view"
    access_change_permission = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Employee)
class EmployeeAdmin(ReadOnlyHrAdmin):
    list_display = ["employee_code", "entity", "status", "employment_basis", "nationality", "hire_date"]
    list_filter = ["status", "employment_basis"]
    search_fields = ["employee_code"]
    exclude = [*Employee.PERSONAL_FIELDS, "id_number_hash"]
    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is None:
            return fields

        def personal_data(employee):  # bound to this request: masked or logged by the service (R10)
            record = services.employee_record(employee, user=request.user)
            return format_html_join("", "<div>{}: {}</div>",
                                    ((name, record[name]) for name in Employee.PERSONAL_FIELDS))

        personal_data.short_description = "Personal data / البيانات الشخصية"
        return [*fields, personal_data]


@admin.register(EmployeeDocument)
class EmployeeDocumentAdmin(ReadOnlyHrAdmin):
    list_display = ["employee", "doc_type", "expiry_date", "superseded_by"]
    exclude = ["number"]


for model, columns in (
    (Department, ["code", "name_en", "name_ar", "entity", "head", "is_active"]),
    (Grade, ["code", "name_en", "name_ar", "entity", "overtime_eligible", "is_active"]),
    (SalaryComponent, ["code", "name_en", "name_ar", "entity", "counts_for_gosi", "counts_for_eosb"]),
    (EmploymentContract, ["employee", "version_no", "status", "contract_type", "effective_date", "entity"]),
    (Assignment, ["reference", "employee", "kind", "status", "start_date", "end_date", "receiving_entity"]),
    (AssignmentEvent, ["assignment", "kind", "end_date", "delivery_status", "attempts"]),
    (LeaveType, ["code", "name_en", "name_ar", "entity", "statutory", "paid_rule"]),
    (LeaveRequest, ["employee", "leave_type", "start_date", "end_date", "days", "status"]),
    (LeaveLedger, ["employee", "leave_type", "date", "kind", "days"]),
    (Termination, ["employee", "kind", "last_working_day", "status"]),
    (EosbCalculation, ["employee", "kind", "as_of", "award", "currency"]),
    (FinalSettlementInput, ["employee", "last_working_day", "eosb_award", "leave_balance_days", "status"]),
    (HrSettings, ["entity", "jurisdiction", "overtime_approval_route", "has_employees"]),
):
    admin.site.register(model, type(f"{model.__name__}Admin", (ReadOnlyHrAdmin,), {"list_display": columns}))


@admin.register(HrAlert)
class HrAlertAdmin(ReadOnlyHrAdmin):
    list_display = ["recipient", "kind", "text_en", "text_ar", "created_at"]

    def get_queryset(self, request):  # own alerts only
        return super().get_queryset(request).filter(recipient=request.user)


@admin.register(StatutoryParameter)
class StatutoryParameterAdmin(admin.ModelAdmin):
    """Global reference data, read-only; entered and verified through hr.services (R7)."""

    list_display = ["jurisdiction", "code", "valid_from", "valid_to", "status", "source_reference"]
    list_filter = ["jurisdiction", "status"]

    def has_module_permission(self, request):
        from core import access
        return access.entities_for(request.user, "hr.view").exists()

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
