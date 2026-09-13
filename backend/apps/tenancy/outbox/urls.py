from django.urls import path

from apps.tenancy.outbox.views import MailboxTestView, MailboxView

app_name = "outbox"

urlpatterns = [
    path("mailbox/", MailboxView.as_view(), name="mailbox"),
    path("mailbox/test/", MailboxTestView.as_view(), name="mailbox-test"),
]
