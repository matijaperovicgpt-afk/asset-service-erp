"""URL configuration for the public client portal.

Two pages only: the report form and the thank-you page. There is no
tracking page and no online quote decision - after submitting, the
client is reached through the notification channels staff operate.
"""

from django.urls import path

from apps.portal import views

app_name = "portal"

urlpatterns = [
    path("", views.ReportCreateView.as_view(), name="report"),
    path("thanks/", views.thanks_view, name="thanks"),
]
