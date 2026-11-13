"""Template helpers for rendering form fields.

Only one thing here needs a tag rather than plain template syntax:
Bootstrap signals a failed field with an ``is-invalid`` class *on the
widget*, and ``{{ field }}`` renders the widget with whatever attrs the
form gave it - there is no way to add one from the template. ``bs_field``
re-renders the widget with the class appended.
"""

from django import template
from django.template.loader import render_to_string

register = template.Library()


@register.simple_tag
def bs_field(field, **extra_attrs):
    """Render ``field``'s widget, marking it ``is-invalid`` when it failed.

    ``as_widget(attrs=...)`` *replaces* same-named attributes rather than
    merging them, so the widget's own class list has to be carried over by
    hand or every field would lose its ``form-control``.
    """
    attrs = {k.replace("_", "-"): v for k, v in extra_attrs.items()}
    classes = [
        field.field.widget.attrs.get("class", ""),
        attrs.pop("class", ""),
        "is-invalid" if field.errors else "",
    ]
    css = " ".join(part for part in classes if part)
    if css:
        attrs["class"] = css
    return field.as_widget(attrs=attrs or None)


@register.filter
def field_named(form, name):
    """The BoundField called ``name``.

    ``form["x"]`` has no template syntax, so a template that needs to walk
    a list of field names - rather than the whole form - cannot reach the
    fields without this.
    """
    return form[name]


@register.simple_block_tag(takes_context=True, end_name="endsection")
def section(context, content, anchor, title, secondary=None):
    """One collapsible case-detail section.

        {% section "acc-quotes" _("Quotes") %}...{% endsection %}

    Every section on the case page used to hand-write this wrapper - the
    guide-section div, the toggle button, the collapse div, the card - and
    there were about eighteen copies of it across the four phase
    templates. Three things went wrong in the copies, and are fixed here
    once:

    * ``aria-expanded`` was hard-coded ``"false"`` while the panel beside
      it was conditionally rendered open, so the server HTML told assistive
      tech the opposite of what was on screen. ``app.js`` reconciled it
      after load, which is a patch over markup that could simply be right.
    * The card repeated the title in a ``card-header``, which the
      stylesheet then hid again (``.guide-section .card > .card-header``).
    * A section holding a form that has just been rejected has to open,
      or the user is redirected to a page where their errors are folded
      out of sight. Only this tag knows about ``invalid_anchor``.

    ``secondary`` overrides the focus-mode decision for sections the guide
    cannot speak for.
    """
    guide = context.get("guide") or {}
    relevant = guide.get("relevant_anchors") or ()
    # guide["hero"] is a GuideStep dataclass, not a dict.
    hero = getattr(guide.get("hero"), "anchor", None)
    rejected = context.get("invalid_anchor") == anchor
    if secondary is None:
        secondary = bool(guide.get("focus")) and anchor not in relevant
    # INV-15: a closed case takes no further changes, so its sections
    # render inert. The lock goes on the section's CONTENT and not on the
    # column around it, because the toggle that opens the section is
    # itself a <button> - a fieldset wrapping the whole column disables
    # that too, and a closed case can then never be expanded to read.
    # Every write form on the case page is inside one of these sections;
    # the activity log and the attachment upload live in the right-hand
    # column, which is exactly the pair that stays usable after closing.
    case = context.get("case")
    locked = bool(getattr(getattr(case, "status", None), "is_terminal", False))
    return render_to_string(
        "backoffice/includes/_section.html",
        {
            "anchor": anchor,
            "title": title,
            "content": content,
            "open": rejected or hero == anchor,
            # A rejected form outranks focus mode: it must be reachable.
            "secondary": secondary and not rejected,
            "locked": locked,
        },
    )
