"""Views for the public client portal.

Public client portal - no accounts, no tracking page. Clients submit a
service report anonymously and see a thank-you page; everything after
that (the "in service" / "finished" status mails, prices, appointments)
reaches them through the notification channels staff operate, never
through a page on this site.
"""

from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.generic import FormView
from django_ratelimit.decorators import ratelimit

from apps.portal import forms as forms_module
from apps.portal.forms import PublicReportForm
from apps.portal.services import create_public_report

#: POR-04: at most this many report submissions per IP per hour.
REPORT_RATE_LIMIT = "5/h"


@method_decorator(
    ratelimit(key="ip", rate=REPORT_RATE_LIMIT, method="POST"), name="post"
)
class ReportCreateView(FormView):
    """Public report submission: creates a SERVICE Case from the form."""

    template_name = "portal/report_form.html"
    form_class = PublicReportForm

    def get_context_data(self, **kwargs):
        """Publish the POR-05 downscaling limits to the template.

        One home for the numbers: the browser reads them from data-
        attributes rather than hard-coding a second copy that could drift
        from the server-side validators.
        """
        context = super().get_context_data(**kwargs)
        context.update(
            max_upload_count=forms_module.MAX_UPLOAD_COUNT,
            downscale_max_edge=forms_module.DOWNSCALE_MAX_EDGE,
            downscale_quality=forms_module.DOWNSCALE_QUALITY,
            downscale_min_bytes=forms_module.DOWNSCALE_MIN_BYTES,
        )
        return context

    def post(self, request, *args, **kwargs):
        form = self.get_form()
        # Honeypot check happens before validation so bots always get a
        # success page and never learn they were detected (see the form
        # docstring for the rationale behind fake success). The thank-you
        # page is identical for a real submission, so nothing about the
        # response says which one this was.
        if form.is_honeypot_triggered():
            return render(request, "portal/thanks.html")
        if form.is_valid():
            return self.form_valid(form)
        return self.form_invalid(form)

    def form_valid(self, form):
        data = form.cleaned_data
        create_public_report(
            contact_name=data["contact_name"],
            phone=data["phone"],
            email=data.get("email") or "",
            asset_info=form.composed_asset_info(),
            location=data.get("location") or "",
            symptoms_description=data["symptoms_description"],
            client_diagnosis=data.get("client_diagnosis") or "",
            photos=data.get("photos") or [],
        )
        return redirect(reverse("portal:thanks"))


def thanks_view(request):
    """Thank-you page shown after a report submission.

    Deliberately static and case-independent: there is no tracking link
    to hand out, so the page names nothing about the case - not even its
    public token. The client hears from staff next.
    """
    return render(request, "portal/thanks.html")
