"""Row locks for check-then-set services.

``serialised_per_case`` in ``apps.cases.services`` locks the *case* row for
services that insert "one per case" rows. The services here stamp a field
on the row they are handed (a quote, an order, a record): two submits used
to both pass the "not yet stamped" check, then both stamp, log and notify.
Kept free of app imports so every department app can use it at module level.
"""

import functools

from django.db import transaction


def lock_row(obj) -> None:
    """Lock ``obj``'s row for the current transaction and reload it, so the
    check that follows judges the stored state, not the caller's snapshot."""
    list(
        type(obj)._default_manager.select_for_update()
        .filter(pk=obj.pk)
        .values_list("pk", flat=True)
    )
    obj.refresh_from_db()


def serialised_per_row(func):
    """Run ``func(obj, ...)`` in a transaction holding ``obj``'s row lock."""

    @functools.wraps(func)
    def wrapper(obj, *args, **kwargs):
        with transaction.atomic():
            lock_row(obj)
            return func(obj, *args, **kwargs)

    return wrapper
