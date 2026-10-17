from django.db import transaction
from django.urls import path

from . import views



def _own_tx(view):
    """Approval actions manage their own transactions (D11 rollback + failure record committed separately),
    so they opt out of ATOMIC_REQUESTS."""
    return transaction.non_atomic_requests(view.as_view())


urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("approvals/requests", views.RequestListView.as_view(), name="requests"),
    path("approvals/requests/<int:pk>", views.RequestDetailView.as_view(), name="request-detail"),
    path("approvals/requests/<int:pk>/approve", _own_tx(views.ApproveView), name="request-approve"),
    path("approvals/requests/<int:pk>/reject", _own_tx(views.RejectView), name="request-reject"),
    path("approvals/requests/<int:pk>/return", _own_tx(views.ReturnView), name="request-return"),
    path("approvals/requests/<int:pk>/cancel", _own_tx(views.CancelView), name="request-cancel"),
    path("approvals/summary", views.SummaryView.as_view(), name="summary"),
    path("approvals/self-approval-report", views.SelfApprovalReportView.as_view(), name="self-approval-report"),
    path("approvals/delegations", views.DelegationListView.as_view(), name="delegations"),
    path("approvals/delegations/<int:pk>", views.DelegationDetailView.as_view(), name="delegation-detail"),
    path("approvals/policies", views.PolicyListView.as_view(), name="policies"),
    path("approvals/policies/<int:pk>", views.PolicyDetailView.as_view(), name="policy-detail"),
    path("notifications", views.NotificationListView.as_view(), name="notifications"),
    path("notifications/<int:pk>/read", views.NotificationReadView.as_view(), name="notification-read"),
    path("devices", views.DeviceView.as_view(), name="devices"),
    path("audit", views.AuditView.as_view(), name="audit"),
    path("access/review", views.AccessReviewView.as_view(), name="access-review"),
]
