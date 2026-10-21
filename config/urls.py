"""URL configuration: Django admin (technical tasks, superusers) and the web UI (docs/specs/web.md)."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("web.urls")),
]

# Bilingual error pages without stack traces (web.md R25).
handler400 = "web.views.bad_request"
handler403 = "web.views.permission_denied"
handler404 = "web.views.page_not_found"
handler500 = "web.views.server_error"
