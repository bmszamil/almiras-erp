"""URLs of the web UI (web.md R4, R23): entity codes and integer primary keys only, never personal data."""
from django.urls import path, register_converter

from . import views


class EntityCodeConverter:
    regex = r"[A-Za-z0-9_-]{1,8}"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(EntityCodeConverter, "entity")

app_name = "web"

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("language/", views.language_view, name="language"),
    path("switch-entity/", views.switch_entity, name="switch_entity"),
    path("e/<entity:entity_code>/", views.dashboard, name="dashboard"),
    path("e/<entity:entity_code>/ledger/entries/", views.journal_entries, name="journal_entries"),
    path("e/<entity:entity_code>/ledger/entries/<int:pk>/", views.journal_entry, name="journal_entry"),
    path("e/<entity:entity_code>/sales/documents/", views.sales_documents, name="sales_documents"),
    path("e/<entity:entity_code>/sales/documents/<int:pk>/", views.sales_document, name="sales_document"),
]
