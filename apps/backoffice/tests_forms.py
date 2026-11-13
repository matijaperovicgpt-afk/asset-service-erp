"""The shared form-rendering layer.

Three things used to be decided per form and per template, and each of
them was wrong somewhere:

* Bootstrap classes, hand-typed about 115 times across 31 form classes in
  three incompatible styles - so a checkbox on either intake page rendered
  unclassed and the intake pages rendered full-size inputs beside
  identical ``-sm`` ones. ``BootstrapFormMixin`` decides them from the
  widget type instead.
* A failed POST redirected and threw the user's input away, flashing
  ``form.errors.as_text()`` - a blob naming raw field names. ``FormInvalid``
  carries the form across the redirect so the errors land on their fields.
* Native date inputs were handed a localised value under ``sr-latn``,
  which they cannot parse, so a stored date rendered as an empty box.
"""

import datetime
from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import translation

from apps.backoffice.formmixins import BootstrapFormMixin
from apps.backoffice.forms import CommaDecimalField, SitePickupForm
from apps.backoffice.widgets import (
    BootstrapRadioSelect,
    FiveMinuteDateTimeWidget,
    PriceItemCheckboxSelect,
)
from apps.cases.models import Case, CaseType, ReportChannel, Status

User = get_user_model()


class DemoForm(BootstrapFormMixin, forms.Form):
    """Covers one field of every shape the mixin has to decide about."""

    text = forms.CharField(label="Text")
    note = forms.CharField(
        label="Note", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )
    pick = forms.ChoiceField(label="Pick", choices=[("a", "A"), ("b", "B")])
    many = forms.MultipleChoiceField(
        label="Many", required=False, choices=[("a", "A")]
    )
    radio = forms.ChoiceField(
        label="Radio", choices=[("x", "X"), ("y", "Y")], widget=forms.RadioSelect
    )
    flag = forms.BooleanField(label="Flag", required=False)
    when = forms.DateField(
        label="When", widget=forms.DateInput(attrs={"type": "date"})
    )
    upload = forms.FileField(label="Upload", required=False)
    hidden = forms.CharField(widget=forms.HiddenInput(), required=False)
    slot = forms.DateTimeField(label="Slot", widget=FiveMinuteDateTimeWidget())
    keeps_own = forms.CharField(
        label="Keeps own",
        widget=forms.TextInput(attrs={"class": "form-control-plaintext form-control"}),
    )
    extra_class = forms.DecimalField(
        label="Extra", widget=forms.NumberInput(attrs={"class": "price-item-hours"})
    )


class BootstrapFormMixinTests(TestCase):
    def setUp(self):
        self.form = DemoForm()

    def _css(self, name):
        return self.form.fields[name].widget.attrs.get("class")

    def test_class_follows_the_widget_type(self):
        self.assertEqual(self._css("text"), "form-control form-control-sm")
        self.assertEqual(self._css("note"), "form-control form-control-sm")
        self.assertEqual(self._css("upload"), "form-control form-control-sm")
        self.assertEqual(self._css("pick"), "form-select form-select-sm")
        self.assertEqual(self._css("many"), "form-select form-select-sm")
        self.assertEqual(self._css("flag"), "form-check-input")

    def test_hidden_inputs_get_no_class(self):
        self.assertIsNone(self._css("hidden"))

    def test_size_is_a_form_level_choice(self):
        class FullSize(DemoForm):
            bootstrap_size = None

        self.assertEqual(
            FullSize().fields["text"].widget.attrs["class"], "form-control"
        )

    def test_an_explicit_bootstrap_class_is_left_alone(self):
        self.assertEqual(
            self._css("keeps_own"), "form-control-plaintext form-control"
        )

    def test_a_non_bootstrap_class_is_kept_and_added_to(self):
        self.assertEqual(
            self._css("extra_class"), "form-control form-control-sm price-item-hours"
        )

    def test_widgets_that_manage_their_own_classes_opt_out(self):
        # Both document, at length, why a class on the container breaks them.
        self.assertTrue(FiveMinuteDateTimeWidget.skip_auto_class)
        self.assertTrue(PriceItemCheckboxSelect.skip_auto_class)
        self.assertIsNone(self.form.fields["slot"].widget.attrs.get("class"))


class FiveMinuteWidgetInvalidTests(TestCase):
    """The rejected state is the one that has to look right.

    ``MultiWidget`` copies the attrs it is handed down onto every half, and
    ``build_attrs`` is ``{**own, **incoming}`` - so the "is-invalid" that
    ``bs_field`` adds used to REPLACE each half's form-control /
    form-select. The control lost its border, chevron and sizing at exactly
    the moment the user had to fix it, and since Bootstrap scopes its red
    styling as ``.form-control.is-invalid`` the class it did carry painted
    nothing.
    """

    def _render(self, extra_class=None):
        widget = FiveMinuteDateTimeWidget()
        attrs = {"id": "id_scheduled_for"}
        if extra_class:
            attrs["class"] = extra_class
        return widget.render("scheduled_for", "2026-09-01T08:30", attrs)

    def test_the_halves_keep_their_classes_when_rejected(self):
        html = self._render("is-invalid")
        self.assertIn('class="form-control form-control-sm is-invalid"', html)
        self.assertIn('class="form-select form-select-sm is-invalid"', html)

    def test_the_valid_render_is_unchanged(self):
        html = self._render()
        self.assertIn('class="form-control form-control-sm"', html)
        self.assertIn('class="form-select form-select-sm"', html)
        self.assertNotIn("is-invalid", html)

    def test_the_wrapper_still_carries_no_class_of_its_own(self):
        # skip_auto_class exists so the mixin does not class the container;
        # pulling the incoming class out per half must not undo that.
        self.assertIsNone(FiveMinuteDateTimeWidget().attrs.get("class"))


class BootstrapRadioSelectTests(TestCase):
    def test_a_plain_radioselect_is_swapped_for_the_bootstrap_one(self):
        self.assertIsInstance(
            DemoForm().fields["radio"].widget, BootstrapRadioSelect
        )

    def test_options_carry_form_check_input_and_the_container_does_not(self):
        html = str(DemoForm()["radio"])
        # Two radios, each classed; the wrapper is a .radio-group, not a
        # 1em x 1em check box - which is what a container-level
        # form-check-input turns it into.
        self.assertEqual(html.count('class="form-check-input"'), 2)
        self.assertIn('class="radio-group', html)
        self.assertNotIn('class="radio-group form-check-input', html)

    def test_choices_survive_the_swap(self):
        html = str(DemoForm()["radio"])
        self.assertIn('value="x"', html)
        self.assertIn('value="y"', html)
        self.assertIn(">X<", html)


class LocalisedDateInputTests(TestCase):
    """sr-latn writes 18.08.2026., which a native date input cannot read.

    Django localises a widget's initial value, so the browser was handed a
    string it silently discarded: the box rendered empty over a stored
    date, and saving the form back cleared it.
    """

    def test_iso_value_under_sr_latn(self):
        with translation.override("sr-latn"):
            html = str(DemoForm(initial={"when": datetime.date(2026, 8, 18)})["when"])
        self.assertIn('value="2026-08-18"', html)
        self.assertNotIn("18.08.2026", html)

    def test_a_modelform_bound_to_an_instance_shows_its_date(self):
        form = SitePickupForm(initial={"lift_out_date": datetime.date(2026, 8, 18)})
        with translation.override("sr-latn"):
            self.assertIn('value="2026-08-18"', str(form["lift_out_date"]))


class CommaDecimalFieldTests(TestCase):
    """People type 4200,00. The view that read this off request.POST
    normalised the separator itself; the field does it now."""

    def test_comma_is_accepted(self):
        self.assertEqual(
            CommaDecimalField().clean("4200,00"), Decimal("4200.00")
        )

    def test_dot_still_works(self):
        self.assertEqual(CommaDecimalField().clean("4200.00"), Decimal("4200.00"))

    def test_nonsense_is_still_refused(self):
        with self.assertRaises(forms.ValidationError):
            CommaDecimalField().clean("not a price")


class FieldPartialTests(TestCase):
    """backoffice/includes/_field.html."""

    def _render(self, form, name, **extra):
        return render_to_string(
            "backoffice/includes/_field.html", {"field": form[name], **extra}
        )

    def test_label_is_associated_with_the_input(self):
        html = self._render(DemoForm(), "text")
        self.assertIn('<label class="form-label" for="id_text"', html)

    def test_required_marker_is_derived_not_typed(self):
        html = self._render(DemoForm(), "text")
        self.assertIn('<span class="req"', html)
        # ...and absent where the field is optional.
        self.assertNotIn('<span class="req"', self._render(DemoForm(), "note"))

    def test_errors_render_on_the_field(self):
        form = DemoForm(data={})
        form.is_valid()
        html = self._render(form, "text")
        self.assertIn("is-invalid", html)
        self.assertIn("invalid-feedback", html)
        self.assertIn("This field is required.", html)

    def test_error_container_id_matches_the_aria_reference(self):
        # Django writes aria-describedby="id_text_error" onto the widget
        # itself; any other id leaves that reference dangling.
        form = DemoForm(data={})
        form.is_valid()
        html = self._render(form, "text")
        self.assertIn('aria-describedby="id_text_error"', html)
        self.assertIn('id="id_text_error"', html)

    def test_a_group_uses_a_fieldset_since_it_has_no_single_input(self):
        html = self._render(DemoForm(), "radio")
        self.assertIn("<fieldset", html)
        self.assertIn("<legend", html)
        # id_for_label is "" for a radio group, so a <label for> would
        # point at nothing.
        self.assertNotIn('for=""', html)

    def test_a_single_checkbox_puts_its_label_after_the_input(self):
        html = self._render(DemoForm(), "flag")
        self.assertIn('class="form-check', html)
        self.assertLess(html.index("<input"), html.index("<label"))

    def test_label_sr_only_keeps_the_accessible_name(self):
        html = self._render(DemoForm(), "text", label_sr_only=True)
        self.assertIn("visually-hidden", html)
        self.assertIn('for="id_text"', html)

    def test_label_sr_only_hides_the_help_text_but_keeps_its_id(self):
        # A model help_text printed inside an 8rem table cell made every
        # parts-catalogue row twelve lines tall. Hidden, not dropped: the
        # widget's aria-describedby still has to resolve.
        class HelpForm(BootstrapFormMixin, forms.Form):
            price = forms.DecimalField(label="Price", help_text="Per unit, EUR.")

        html = self._render(HelpForm(), "price", label_sr_only=True)
        self.assertIn('class="form-text visually-hidden" id="id_price_helptext"', html)
        self.assertIn('aria-describedby="id_price_helptext"', html)
        # ...and still visible where the label is.
        plain = self._render(HelpForm(), "price")
        self.assertIn('class="form-text" id="id_price_helptext"', plain)

    def test_no_template_comment_leaks_into_the_output(self):
        # {# ... #} is single-line only; a multi-line one renders verbatim.
        for name in ("text", "radio", "flag"):
            self.assertNotIn("{#", self._render(DemoForm(), name))


class InvalidFormRoundTripTests(TestCase):
    """A rejected form comes back with the user's input and its errors.

    Before this, every action redirected and flashed
    ``form.errors.as_text()``: the typed values were gone and the message
    named raw field names ("* amount"), attached to nothing.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="mgr", password="pw", is_staff=True, role="MANAGER"
        )
        self.status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="new_report",
            name="New report",
            client_visible_name="Received",
            sort_order=1,
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
            raw_contact_name="Ana",
            symptoms_description="knocking",
        )
        self.client.force_login(self.user)
        self.detail_url = reverse("backoffice:case-detail", args=[self.case.pk])

    def _submit_bad_quote(self):
        return self.client.post(
            reverse("backoffice:quote-create", args=[self.case.pk]),
            {"description": "Frame work", "amount": "", "valid_until": ""},
            follow=True,
        )

    def test_the_typed_values_survive(self):
        response = self._submit_bad_quote()
        self.assertContains(response, "Frame work")

    def test_the_error_lands_on_the_field(self):
        response = self._submit_bad_quote()
        self.assertContains(response, "invalid-feedback")

    def test_no_raw_field_names_in_the_message(self):
        response = self._submit_bad_quote()
        # as_text() rendered "* amount" / "* description"; nothing should
        # surface a field name that is not a label any more.
        self.assertNotContains(response, "* amount")

    def test_the_section_holding_it_is_open(self):
        response = self._submit_bad_quote()
        # Otherwise the user is returned to a page where their errors are
        # folded away inside a collapsed accordion.
        self.assertContains(response, 'id="acc-quotes" class="collapse show"')

    def test_the_stash_is_consumed_once(self):
        self._submit_bad_quote()
        # A later visit is a clean page, not a page still showing errors.
        response = self.client.get(self.detail_url)
        self.assertNotContains(response, "invalid-feedback")

    def test_a_stash_from_another_case_is_not_claimed(self):
        other = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=self.status,
            raw_contact_name="Bob",
            symptoms_description="leak",
        )
        self._submit_bad_quote()
        response = self.client.get(
            reverse("backoffice:case-detail", args=[other.pk])
        )
        self.assertNotContains(response, "Frame work")


class ApprovalLevelSelectTests(TestCase):
    """The approval selector offers the levels the quote still needs.

    It used to loop over an ``approval_levels`` context variable that the
    2026-08-14 hardening had replaced with a per-quote
    ``missing_level_choices`` and stopped providing - so the select
    rendered with no options at all and no approval could be recorded
    from the case page.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="mgr2", password="pw", is_staff=True, role="MANAGER"
        )
        status = Status.objects.create(
            case_type=CaseType.SERVICE,
            code="awaiting_approval",
            name="Awaiting approval",
            client_visible_name="Awaiting approval",
            sort_order=5,
        )
        self.case = Case.objects.create(
            case_type=CaseType.SERVICE,
            report_channel=ReportChannel.PHONE,
            status=status,
            raw_contact_name="Ana",
            symptoms_description="knocking",
        )
        self.client.force_login(self.user)

    def test_a_sent_quote_offers_at_least_one_level(self):
        from apps.quotes.models import Quote
        from apps.quotes.services import create_quote, send_quote

        quote = create_quote(
            self.case, amount=Decimal("100"), description="x", user=self.user
        )
        send_quote(quote, self.user)
        response = self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        start = html.index('<select name="level"')
        select = html[start : html.index("</select>", start)]
        self.assertIn("<option", select)
        self.assertEqual(quote.status, Quote.Status.SENT)


class SerbianRenderTests(TestCase):
    """The case page in the language it actually runs in.

    Production serves sr-latn; the suite runs in English so assertions on
    template text stay readable. Anything that only breaks under the real
    locale - a localised number in an attribute, a date the browser cannot
    read - therefore needs its own check.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="sr", password="pw", is_staff=True, role="MANAGER"
        )
        status = Status.objects.create(
            case_type=CaseType.STORAGE,
            code="inquiry",
            name="Upit",
            client_visible_name="Received",
            sort_order=0,
        )
        self.case = Case.objects.create(
            case_type=CaseType.STORAGE,
            report_channel=ReportChannel.PHONE,
            status=status,
            raw_contact_name="Ana",
            symptoms_description="storage",
        )
        self.client.force_login(self.user)

    def _get(self):
        self.client.cookies["django_language"] = "sr-latn"
        return self.client.get(
            reverse("backoffice:case-detail", args=[self.case.pk])
        )

    def test_the_page_renders(self):
        self.assertEqual(self._get().status_code, 200)

    def test_the_staff_shell_is_the_quiet_one(self):
        # The brand language (Cormorant, brass, the navy hero) is scoped to
        # body.luxe and belongs to the client portal and the sign-in page.
        # Putting it on a tool somebody stares at all day read as shouting.
        response = self._get()
        self.assertContains(response, 'class="admin"')
        self.assertNotContains(response, "luxe")

    def test_no_template_comment_leaks_into_the_page(self):
        # {# ... #} is single-line only; several multi-line ones were
        # rendering internal rule IDs into the HTML.
        html = self._get().content.decode()
        self.assertNotIn("{#", html)

    def test_no_internal_rule_tags_are_shown_to_the_user(self):
        html = self._get().content.decode()
        for tag in ("WNT-01", "WNT-03", "WNT-06", "TRP-01", "TRP-03", "TRP-08"):
            self.assertNotIn(tag, html)


class CheckboxSelectMultipleSwapTests(TestCase):
    """A declared multi-checkbox must stay a multi-checkbox.

    ``CheckboxSelectMultiple`` is a *subclass* of ``RadioSelect`` in
    Django, so the mixin's ``isinstance(widget, forms.RadioSelect)`` test
    matched it and swapped in the single-valued ``BootstrapRadioSelect``:
    ``allow_multiple_selected`` went False, ``value_from_datadict``
    returned only the last ticked value, and ``ModelMultipleChoiceField``
    then rejected it with "Enter a list of values."

    The one multi-checkbox that predates this - the quote form's
    price-list picker - escaped only by accident of ordering, being added
    after ``super().__init__()`` had already run the mixin. Moving it to
    the class body would have broken it silently.
    """

    class MultiForm(BootstrapFormMixin, forms.Form):
        picks = forms.MultipleChoiceField(
            choices=[("a", "A"), ("b", "B"), ("c", "C")],
            widget=forms.CheckboxSelectMultiple,
        )

    def test_the_widget_stays_multi_valued(self):
        widget = self.MultiForm().fields["picks"].widget
        self.assertTrue(widget.allow_multiple_selected)
        self.assertEqual(widget.input_type, "checkbox")

    def test_every_ticked_value_survives_the_post(self):
        form = self.MultiForm(QueryDict("picks=a&picks=c"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["picks"], ["a", "c"])

    def test_a_radio_group_is_still_swapped(self):
        from apps.backoffice.widgets import BootstrapRadioSelect

        class RadioForm(BootstrapFormMixin, forms.Form):
            pick = forms.ChoiceField(
                choices=[("a", "A")], widget=forms.RadioSelect
            )

        widget = RadioForm().fields["pick"].widget
        self.assertIsInstance(widget, BootstrapRadioSelect)
        self.assertFalse(widget.allow_multiple_selected)
