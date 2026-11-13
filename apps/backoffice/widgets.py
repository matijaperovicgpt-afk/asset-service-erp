"""Form widgets shared by the backoffice forms."""

import json

from django import forms
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.scheduling import workinghours

# Appointments are booked on a five-minute grid. A native
# ``datetime-local`` input cannot enforce that in the picker itself: the
# ``step`` attribute only makes the browser reject an off-grid minute
# *after* the user has typed it, which is a validation error instead of a
# choice. The slots are therefore offered as a select, so an off-grid
# minute is simply not on the menu.
MINUTE_STEP = workinghours.MINUTE_STEP

SLOT_CHOICES = [
    (f"{hour:02d}:{minute:02d}", f"{hour:02d}:{minute:02d}")
    for hour in range(24)
    for minute in range(0, 60, MINUTE_STEP)
]

#: The same grid narrowed to the yard's opening hours - one menu for the
#: whole week, narrowed again per weekday in the browser. The server-side
#: rule is ``scheduling.services.book_appointment``; this only keeps the
#: menu from offering times that rule would refuse.
WORKING_SLOT_CHOICES = [(slot, slot) for slot in workinghours.all_slots()]

EMPTY_SLOT = ("", "--:--")


class FiveMinuteDateTimeWidget(forms.MultiWidget):
    """A date input plus a select of five-minute time slots.

    Submits the two halves as one ``YYYY-MM-DDTHH:MM`` value, i.e. the
    same string a ``datetime-local`` input posts, so the fields keep
    their existing ``input_formats``. A flat value posted under the bare
    field name (older clients, tests) is still accepted.

    With ``working_hours=True`` (the default) the menu carries only slots
    inside the yard's opening hours, and the wrapper publishes the
    per-weekday windows so the browser can narrow it further once a date
    is picked - a Sunday ends up with no slots at all. This is the
    courtesy layer: ``scheduling.services.book_appointment`` refuses an
    out-of-hours time whatever the form posts. Pass
    ``working_hours=False`` for a time that is not the site's to keep
    (an external carrier's loading window, TRP-02).
    """

    template_name = "backoffice/widgets/datetime_5min.html"

    # Sizing is decided below, per half; BootstrapFormMixin must not also
    # write a class onto the MultiWidget wrapper.
    skip_auto_class = True

    def __init__(self, attrs=None, working_hours=True):
        self.working_hours = working_hours
        shared = dict(attrs or {})
        shared.pop("class", None)
        shared.pop("type", None)
        shared.pop("step", None)
        date_attrs = {
            **shared,
            "type": "date",
            "class": "form-control form-control-sm",
        }
        if working_hours:
            # Shown by app.js as the date input's tooltip once a
            # closed day is picked; translated here, not in the JS.
            date_attrs["data-closed-hint"] = _(
                "The site is closed that day. %(hours)s"
            ) % {"hours": workinghours.summary()}
        time_attrs = {
            **shared,
            "class": "form-select form-select-sm",
            "aria-label": _("Time"),
        }
        super().__init__(
            [
                forms.DateInput(attrs=date_attrs, format="%Y-%m-%d"),
                forms.Select(attrs=time_attrs, choices=self._base_choices()),
            ],
            attrs=shared,
        )

    def _base_choices(self):
        """The menu before a stored off-menu value is folded back in."""
        slots = WORKING_SLOT_CHOICES if self.working_hours else SLOT_CHOICES
        return [EMPTY_SLOT, *slots]

    def decompress(self, value):
        if not value:
            return [None, None]
        if isinstance(value, str):
            # Redisplay after a failed submit: the value is the combined
            # "YYYY-MM-DDTHH:MM" string this widget itself produced.
            date_part, _sep, time_part = value.partition("T")
            return [date_part, time_part[:5]]
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return [value.strftime("%Y-%m-%d"), value.strftime("%H:%M")]

    def get_context(self, name, value, attrs):
        attrs = dict(attrs or {})
        # ``MultiWidget`` copies the final attrs down to every subwidget, and
        # ``Widget.build_attrs`` is ``{**own, **incoming}`` - so a "class"
        # arriving from the caller (``bs_field`` adds "is-invalid" on a
        # rejected field) REPLACES each half's own form-control/form-select
        # instead of adding to it, and the control loses all its styling at
        # exactly the moment the user has to fix it. Take the class out here
        # and append it per half instead.
        extra = attrs.pop("class", "")

        decompressed = value if isinstance(value, list) else self.decompress(value)
        current = decompressed[1] if len(decompressed) > 1 else None
        choices = self._base_choices()
        if current and all(current != slot for slot, _label in choices):
            # A record saved before the grid (or before the opening-hours
            # menu) existed keeps its own time on the menu, so re-saving
            # it does not silently move the appointment.
            choices.insert(1, (current, current))
        self.widgets[1].choices = choices

        context = super().get_context(name, value, attrs)
        if self.working_hours:
            # Read by app.js to narrow the menu to the weekday the
            # date half is showing. Autoescaping renders the quotes as
            # entities, which is what an HTML attribute wants and what
            # JSON.parse gets back.
            context["widget"]["working_hours"] = json.dumps(
                workinghours.windows_by_weekday(), separators=(",", ":")
            )
        if extra:
            for subwidget, widget in zip(
                context["widget"]["subwidgets"], self.widgets
            ):
                subwidget["attrs"]["class"] = " ".join(
                    part
                    for part in (widget.attrs.get("class", ""), extra)
                    if part
                )
        return context

    def value_from_datadict(self, data, files, name):
        date_part = data.get(f"{name}_0")
        time_part = data.get(f"{name}_1")
        if date_part is None and time_part is None:
            # Posted flat, as a datetime-local input would.
            return data.get(name)
        if not date_part and not time_part:
            return ""
        return f"{date_part or ''}T{time_part or ''}"

    def value_omitted_from_data(self, data, files, name):
        return (
            name not in data
            and f"{name}_0" not in data
            and f"{name}_1" not in data
        )


class BootstrapRadioSelect(forms.RadioSelect):
    """A radio group whose *options* carry ``form-check-input``.

    Django renders a radio group through ``multiple_input.html``, which
    copies ``widget.attrs["class"]`` onto the wrapping ``<div>`` as well as
    onto every ``<input>``. Passing ``form-check-input`` at widget level
    therefore styles the container as a 1em x 1em checkbox box - the same
    trap ``PriceItemCheckboxSelect`` below documents at length. The class
    belongs on the options, so that is where this widget puts it.

    The template also emits Bootstrap's ``.form-check`` wrapper per option,
    which the stock one does not: without it a label that wraps runs back
    under its own radio instead of hanging indented.

    ``BootstrapFormMixin`` swaps this in for any bare ``RadioSelect``, so
    forms declare the plain widget and get this one.
    """

    template_name = "backoffice/widgets/radio_bootstrap.html"
    skip_auto_class = True

    def create_option(self, name, value, label, selected, index, **kwargs):
        option = super().create_option(
            name, value, label, selected, index, **kwargs
        )
        option["attrs"]["class"] = "form-check-input"
        return option


class BootstrapCheckboxSelectMultiple(forms.CheckboxSelectMultiple):
    """The multi-select twin of ``BootstrapRadioSelect``.

    It exists because of a trap in Django's own hierarchy:
    ``CheckboxSelectMultiple`` is a *subclass* of ``RadioSelect``, so an
    ``isinstance(widget, forms.RadioSelect)`` test matches it. The mixin
    used to swap any such widget for ``BootstrapRadioSelect`` - which is
    single-valued - so a declared checkbox group silently became a radio
    group: ``allow_multiple_selected`` went False, ``value_from_datadict``
    returned only the last ticked value, and ``ModelMultipleChoiceField``
    then rejected it with "Enter a list of values."

    The one multi-checkbox that predates this escaped only by accident of
    ordering: ``QuoteCreateForm`` adds its price-list picker *after*
    ``super().__init__()`` has already run the mixin, so the field did not
    exist when the swap happened. Moving that field to the class body
    would have broken it.

    Same template as the radio group - the option's ``type`` comes from
    the widget's own ``input_type``, so one template serves both.
    """

    template_name = "backoffice/widgets/radio_bootstrap.html"
    skip_auto_class = True


class PartCheckboxSelect(forms.CheckboxSelectMultiple):
    """The parts-catalogue picker on the diagnostic card (SVC-10).

    Built on the same footing as ``PriceItemCheckboxSelect``, including
    the reason that one exists: Django's ``multiple_input.html`` copies
    ``widget.attrs["class"]`` onto the container as well as every input,
    so ``form-check-input`` at widget level collapses the list. The class
    goes on the options only.

    Every option carries what the SVC-10 decision turns on -
    ``data-consumable`` and ``data-stock`` - so the card can say, before
    the form is posted, whether this selection is heading for the §3.2
    shortcut or the §3.3 quote path. ``data-search`` is the lower-cased
    "code name" the filter box matches against, built server-side because
    a catalogue of a few hundred rows should not be lower-cased on every
    keystroke.
    """

    template_name = "backoffice/widgets/part_checkboxes.html"

    # As with the price-list picker, the container class is the point.
    skip_auto_class = True

    def __init__(self, attrs=None):
        attrs = dict(attrs or {})
        attrs.pop("class", None)
        super().__init__(attrs)
        # {part pk: BoundField} for the per-part quantity inputs, set by
        # the form once all of its fields exist.
        self.quantity_fields = {}

    def create_option(self, name, value, label, selected, index, **kwargs):
        option = super().create_option(
            name, value, label, selected, index, **kwargs
        )
        option["attrs"]["class"] = "form-check-input part-pick"
        part = getattr(value, "instance", None)
        if part is None:
            return option
        option["attrs"]["data-consumable"] = "1" if part.is_consumable else "0"
        option["attrs"]["data-stock"] = str(part.stock_qty)
        # Not in attrs: a template variable cannot address "data-search".
        option["search"] = ("%s %s" % (part.code, part.name)).lower()
        option["part"] = part
        bound = self.quantity_fields.get(part.pk)
        if bound is not None:
            option["quantity_html"] = mark_safe(bound.as_widget())
        return option


class PriceItemCheckboxSelect(forms.CheckboxSelectMultiple):
    """The service price-list picker on the quote-create card.

    Django's ``multiple_input.html`` copies ``widget.attrs["class"]`` onto
    the container ``<div>`` as well as onto every ``<input>``, so passing
    ``class="form-check-input"`` at widget level collapsed the whole list
    into Bootstrap's 1em x 1em checkbox box - the labels broke one word
    per line and the list painted over the rest of the card. The class
    therefore belongs on the options only, which is what this widget and
    its template do; the wrapper keeps its own class.

    Each option also carries the price as ``data-price`` so the amount
    can be totalled client-side (app.js). The price is written with
    ``str()`` rather than through a template filter: ``LANGUAGE_CODE`` is
    ``sr-latn`` in production and a localised "60,00" would be parsed as
    60 by ``parseFloat``.
    """

    template_name = "backoffice/widgets/price_item_checkboxes.html"

    # The container class is the whole point of this widget (see above);
    # BootstrapFormMixin must keep its hands off it.
    skip_auto_class = True

    def __init__(self, attrs=None):
        attrs = dict(attrs or {})
        attrs.pop("class", None)
        super().__init__(attrs)
        # {item pk: BoundField} for the per-hour quantity inputs, set by
        # the form once all of its fields exist. Rendered inline with the
        # checkbox they belong to.
        self.hours_fields = {}

    def create_option(self, name, value, label, selected, index, **kwargs):
        option = super().create_option(
            name, value, label, selected, index, **kwargs
        )
        option["attrs"]["class"] = "form-check-input"
        # ModelChoiceIteratorValue carries the object the choice came from.
        item = getattr(value, "instance", None)
        if item is None:
            return option
        option["attrs"]["data-price"] = str(item.price)
        option["attrs"]["data-name"] = item.name
        if item.unit == item.Unit.PER_HOUR:
            option["attrs"]["data-hourly"] = "1"
        bound = self.hours_fields.get(item.pk)
        if bound is not None:
            option["hours_html"] = mark_safe(bound.as_widget())
        return option
