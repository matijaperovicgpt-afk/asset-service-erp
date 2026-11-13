"""Site working hours - the single source of truth for scheduling.

SVC-04: an appointment is a slot someone at the site has to keep, so
a time the yard is closed is not a slot at all. The hours live here
rather than inside the picker or the service, because both need them and
they must not drift apart: the widget offers only the slots a given
weekday actually has (``slots_for_weekday``), and
``scheduling.services.book_appointment`` refuses everything else
(``is_working_time``) - the menu is the courtesy, the guard is the rule.
"""

from datetime import time

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

#: Opening hours per ``date.weekday()`` (0 = Monday); ``None`` = closed.
#: Both ends are inclusive - "nine to five" means 17:00 sharp is still a
#: slot, and it is the last one of the day.
WORKING_HOURS = {
    0: (time(9, 0), time(17, 0)),
    1: (time(9, 0), time(17, 0)),
    2: (time(9, 0), time(17, 0)),
    3: (time(9, 0), time(17, 0)),
    4: (time(9, 0), time(17, 0)),
    5: (time(8, 0), time(14, 0)),
    6: None,
}

#: The scheduling grid, in minutes. Slots are offered on it and nothing
#: between them is on the menu.
MINUTE_STEP = 5


def as_local(value):
    """``value`` in the yard's wall-clock time (naive values pass through).

    Opening hours are wall-clock hours in settings.TIME_ZONE; a UTC-stored datetime
    has to be read locally before its weekday and time mean anything.
    """
    if value is not None and hasattr(value, "hour") and timezone.is_aware(value):
        return timezone.localtime(value)
    return value


def window_for(value):
    """Return ``(open, close)`` for ``value``'s weekday, or ``None``.

    Accepts a ``date`` or a ``datetime``; an aware datetime is read in
    local time, because opening hours are wall-clock hours at the yard,
    not UTC.
    """
    value = as_local(value)
    if value is None:
        return None
    return WORKING_HOURS.get(value.weekday())


def is_working_time(value) -> bool:
    """Is ``value`` (a datetime) inside the yard's opening hours?"""
    window = window_for(value)
    if window is None:
        return False
    value = as_local(value)
    opens, closes = window
    return opens <= value.time() <= closes


def slots_for_weekday(weekday: int, step: int = MINUTE_STEP) -> list:
    """The ``"HH:MM"`` slots a weekday offers; empty on a closed day."""
    window = WORKING_HOURS.get(weekday)
    if window is None:
        return []
    opens, closes = window
    first = opens.hour * 60 + opens.minute
    last = closes.hour * 60 + closes.minute
    # Start on the grid: an opening time off it (say 08:02) would
    # otherwise seed a whole day of off-grid slots.
    start = -(-first // step) * step
    return [
        "%02d:%02d" % divmod(minutes, 60)
        for minutes in range(start, last + 1, step)
    ]


def all_slots(step: int = MINUTE_STEP) -> list:
    """Every slot any weekday offers, ascending and without duplicates.

    This is what the time select is built from: one menu for the whole
    week, narrowed per weekday in the browser and enforced server-side.
    """
    seen = set()
    for weekday in sorted(WORKING_HOURS):
        for slot in slots_for_weekday(weekday, step):
            seen.add(slot)
    return sorted(seen)


def windows_by_weekday() -> dict:
    """``{weekday: ["HH:MM", "HH:MM"] or None}`` for the browser.

    Deliberately the windows and not the expanded slots: the option
    values are already ``"HH:MM"`` strings, which compare correctly as
    text, so the picker filters against two bounds instead of shipping
    several hundred strings per rendered widget.
    """
    return {
        str(weekday): (
            None
            if window is None
            else [window[0].strftime("%H:%M"), window[1].strftime("%H:%M")]
        )
        for weekday, window in WORKING_HOURS.items()
    }


def next_working_slot(after=None, at=time(10, 0)):
    """The first ``at`` o'clock on an open day strictly after ``after``.

    Handy wherever a sensible default is wanted instead of an arbitrary
    "tomorrow" that may land on a Sunday - and what the tests book, so a
    suite run on a Saturday evening does not fail on the calendar.
    """
    from datetime import timedelta

    moment = as_local(after) if after is not None else timezone.localtime()
    candidate = moment.replace(
        hour=at.hour, minute=at.minute, second=0, microsecond=0
    )
    while candidate <= moment or not is_working_time(candidate):
        candidate = (candidate + timedelta(days=1)).replace(
            hour=at.hour, minute=at.minute, second=0, microsecond=0
        )
    return candidate


def summary() -> str:
    """One-line description of the hours, for errors and help text."""
    return _("Mon-Fri 09:00-17:00, Sat 08:00-14:00; closed on Sunday.")
