"""Exceptions raised by the case workflow state machine."""


class CaseWorkflowError(Exception):
    """Base class for case workflow errors."""


class InvalidTransition(CaseWorkflowError):
    """The requested status transition is not defined in the workflow."""


class GuardCheckFailed(CaseWorkflowError):
    """A precondition (guard) for the requested transition is not met."""


class RoleNotAllowed(CaseWorkflowError):
    """The user's role may not take the requested transition (STM-06)."""


class AppendOnlyViolation(Exception):
    """An attempt to update or delete an append-only audit record (INV-03)."""
