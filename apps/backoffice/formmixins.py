"""Bootstrap styling applied to widgets by type, in one place.

Every field in every backoffice form used to carry its own
``attrs={"class": "form-control form-control-sm"}`` - roughly 115 copies
across 31 form classes, written in three mutually incompatible styles:
per-field ``attrs`` here in ``forms.py``, a ``for field in
self.fields.values()`` loop in ``storage.py``/``transport.py``/``parts.py``,
and nothing at all in ``triage.py``. The loops disagreed with each other
too - the storage/transport pair had no ``CheckboxInput`` branch, so every
checkbox on both intake pages rendered unclassed, and neither added the
``-sm`` suffix the case-detail cards use, so the intake pages rendered
full-size inputs beside identical ``-sm`` ones.

Deciding the class from the widget type removes all of that. A form that
genuinely wants something else still wins: a field whose ``attrs["class"]``
already names a Bootstrap form class is left untouched.
"""

from django import forms

# Not re-exported on ``django.forms``; it is the shared base of DateInput,
# TimeInput and DateTimeInput, i.e. exactly the widgets that localise.
from django.forms.widgets import DateTimeBaseInput

from .widgets import BootstrapCheckboxSelectMultiple, BootstrapRadioSelect

# A class list already naming one of these is a deliberate choice by the
# form, not an oversight - leave it alone.
BOOTSTRAP_MARKERS = frozenset({"form-control", "form-select", "form-check-input"})

# ``<input type="...">`` values whose value a localised format would break.
# ``sr-latn`` renders a date as "18.08.2026.", which a native date input
# cannot parse - it silently shows an empty box over a stored value. The
# widget must therefore emit the ISO format the input actually reads.
#
# Keyed on ``widget.input_type``, not ``widget.attrs["type"]``: ``Input``
# pops ``type`` out of attrs in its constructor and keeps it there instead,
# so a form that declared ``attrs={"type": "date"}`` has no such attr left.
ISO_INPUT_FORMATS = {
    "date": "%Y-%m-%d",
    "datetime-local": "%Y-%m-%dT%H:%M",
    "time": "%H:%M",
}


class BootstrapFormMixin:
    """Give every widget the Bootstrap class its type calls for.

    Put it first in the bases: ``class QuoteForm(BootstrapFormMixin,
    forms.Form)``. Works on ``Form`` and ``ModelForm`` alike.
    """

    #: ``"sm"`` matches the dense case-detail cards, where most forms live.
    #: Full-page forms (case create, intake, triage) set this to ``None``.
    bootstrap_size = "sm"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap_classes()

    def apply_bootstrap_classes(self):
        for field in self.fields.values():
            widget = field.widget

            # A bare RadioSelect renders through ``multiple_input.html``,
            # which copies the widget class onto the wrapping container as
            # well as onto every input - the trap PriceItemCheckboxSelect
            # documents. Swap in the subclass that classes its options.
            #
            # CheckboxSelectMultiple is tested FIRST because it is a
            # subclass of RadioSelect: matched by the RadioSelect branch it
            # was replaced with a single-valued widget, which quietly threw
            # away every ticked option but the last.
            if isinstance(widget, forms.CheckboxSelectMultiple) and not (
                isinstance(widget, BootstrapCheckboxSelectMultiple)
            ):
                widget = _as_bootstrap_choice(
                    field, BootstrapCheckboxSelectMultiple
                )
            elif isinstance(widget, forms.RadioSelect) and not isinstance(
                widget, BootstrapRadioSelect
            ):
                widget = _as_bootstrap_choice(field, BootstrapRadioSelect)

            _apply_iso_format(widget)

            if getattr(widget, "skip_auto_class", False):
                continue

            css = self._widget_class(widget)
            if not css:
                continue
            existing = widget.attrs.get("class", "").split()
            if BOOTSTRAP_MARKERS.intersection(existing):
                continue
            widget.attrs["class"] = " ".join([*css.split(), *existing])

    def _widget_class(self, widget):
        """The Bootstrap class for ``widget``, or ``""`` to leave it bare."""
        if isinstance(widget, forms.HiddenInput):
            return ""
        if isinstance(widget, forms.CheckboxInput):
            # Bootstrap has no size variant for check inputs.
            return "form-check-input"
        if isinstance(widget, forms.CheckboxSelectMultiple):
            # Same container-class trap as RadioSelect; the options are
            # classed by the widget, not from here.
            return ""
        if isinstance(widget, forms.Select):
            # Catches Select, SelectMultiple and NullBooleanSelect;
            # RadioSelect and CheckboxSelectMultiple descend from
            # ChoiceWidget instead and are handled above.
            return self._sized("form-select")
        return self._sized("form-control")

    def _sized(self, base):
        if not self.bootstrap_size:
            return base
        return f"{base} {base}-{self.bootstrap_size}"


def _as_bootstrap_choice(field, widget_class):
    """Replace ``field``'s choice widget with the Bootstrap-aware subclass.

    ``ChoiceField.choices`` writes through to the widget it held at the
    time, so the replacement has to be handed the choices explicitly.
    """
    old = field.widget
    new = widget_class(attrs=dict(old.attrs))
    new.choices = old.choices
    new.is_required = old.is_required
    field.widget = new
    return new


def _apply_iso_format(widget):
    """Force ISO input formats on native date/time inputs.

    Django localises a widget's *initial* value, so under ``sr-latn`` a
    stored date reaches the browser as "18.08.2026." and a native
    ``<input type="date">`` - which only reads ISO - renders blank. The
    stored value is still there; the user simply cannot see it, and
    re-saving the form silently clears it.
    """
    if isinstance(widget, forms.MultiWidget):
        for sub in widget.widgets:
            _apply_iso_format(sub)
        return
    if not isinstance(widget, DateTimeBaseInput):
        return
    iso = ISO_INPUT_FORMATS.get(getattr(widget, "input_type", None))
    if iso and not widget.format:
        widget.format = iso
