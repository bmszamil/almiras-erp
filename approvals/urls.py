from django.urls import path

from . import views

urlpatterns = [
    path("me", views.MeView.as_view(), name="me"),
    path("approvals/requests", views.RequestListView.as_view(), name="requests"),
    path("approvals/requests/<int:pk>", views.RequestDetailView.as_view(), name="request-detail"),
    path("approvals/requests/<int:pk>/approve", views.ApproveView.as_view(), name="request-approve"),
    path("approvals/requests/<int:pk>/reject", views.RejectView.as_view(), name="request-reject"),
    path("approvals/requests/<int:pk>/return", views.ReturnView.as_view(), name="request-return"),
    path("approvals/requests/<int:pk>/cancel", views.CancelView.as_view(), name="request-cancel"),
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
]
