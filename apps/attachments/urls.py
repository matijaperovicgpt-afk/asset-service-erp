"""URL configuration for login-protected media serving (POR-04)."""

from django.urls import re_path

from apps.attachments import views

app_name = "attachments"

urlpatterns = [
    re_path(r"^media/(?P<path>.*)$", views.protected_media, name="protected_media"),
]
