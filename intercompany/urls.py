from django.db import transaction
from django.urls import path

from . import views


def _own_tx(view_cls, **kw):
    """Views whose services commit a record of a refusal or failure (failed billing run, refused issue)
    manage their own transactions, like the approvals action views (ATOMIC_REQUESTS on PostgreSQL)."""
    return transaction.non_atomic_requests(view_cls.as_view(**kw))


urlpatterns = [
    path("service-types", views.ServiceTypeListView.as_view(), name="ic-service-types"),
    path("agreements", views.AgreementListView.as_view(), name="ic-agreements"),
    path("agreements/<int:pk>", views.AgreementDetailView.as_view(), name="ic-agreement"),
    path("agreements/<int:pk>/submit", views.AgreementActionView.as_view(action="submit"), name="ic-agreement-submit"),
    path("agreements/<int:pk>/suspend", views.AgreementActionView.as_view(action="suspend"), name="ic-agreement-suspend"),
    path("agreements/<int:pk>/end", views.AgreementActionView.as_view(action="end"), name="ic-agreement-end"),
    path("timesheets", views.TimesheetListView.as_view(), name="ic-timesheets"),
    path("timesheets/<int:pk>", views.TimesheetDetailView.as_view(), name="ic-timesheet"),
    path("timesheets/<int:pk>/submit", views.TimesheetActionView.as_view(action="submit"), name="ic-timesheet-submit"),
    path("timesheets/<int:pk>/approve", views.TimesheetActionView.as_view(action="approve"), name="ic-timesheet-approve"),
    path("timesheets/<int:pk>/reject", views.TimesheetActionView.as_view(action="reject"), name="ic-timesheet-reject"),
    path("cost-pools", views.CostPoolListView.as_view(), name="ic-cost-pools"),
    path("cost-pools/<int:pk>/compute", views.CostPoolComputeView.as_view(), name="ic-cost-pool-compute"),
    path("billable-items", views.BillableItemListView.as_view(), name="ic-billable-items"),
    path("billing-runs", _own_tx(views.BillingRunListView), name="ic-billing-runs"),
    path("invoices", views.InvoiceListView.as_view(), name="ic-invoices"),
    path("invoices/<int:pk>", views.InvoiceDetailView.as_view(), name="ic-invoice"),
    path("invoices/<int:pk>/submit", views.InvoiceActionView.as_view(action="submit"), name="ic-invoice-submit"),
    path("invoices/<int:pk>/cancel", views.InvoiceActionView.as_view(action="cancel"), name="ic-invoice-cancel"),
    path("invoices/<int:pk>/issue", _own_tx(views.InvoiceActionView, action="issue"), name="ic-invoice-issue"),
    path("invoices/<int:pk>/credit-notes", views.InvoiceActionView.as_view(action="credit-notes"), name="ic-credit-notes"),
    path("bills", views.BillListView.as_view(), name="ic-bills"),
    path("bills/<int:pk>", views.BillDetailView.as_view(), name="ic-bill"),
    path("bills/<int:pk>/submit", views.BillActionView.as_view(action="submit"), name="ic-bill-submit"),
    path("bills/<int:pk>/dispute", views.BillActionView.as_view(action="dispute"), name="ic-bill-dispute"),
    path("bills/<int:pk>/undispute", views.BillActionView.as_view(action="undispute"), name="ic-bill-undispute"),
    path("reconciliation", views.ReconciliationView.as_view(), name="ic-reconciliation"),
    path("related-party", views.RelatedPartyReportView.as_view(), name="ic-related-party"),
]
