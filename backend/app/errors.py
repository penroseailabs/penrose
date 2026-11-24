"""The exceptions the runtime raises and catches, and how an error is put into words for the user."""

import httpx


class ProviderError(RuntimeError):
    """The model provider failed or answered with something unusable."""


class DeviceError(RuntimeError):
    """The device gateway or the bridge could not do what was asked; the action may or may not have happened."""


class PhoneSetupError(RuntimeError):
    """The phone itself is not set up for the agents (no app runs the bridge). Retrying cannot help, so it
    ends the run with a message for the user instead of failing one step after another."""


class Rejected(ValueError):
    """A decision the runtime cannot apply. The agent that made it is told why and decides again."""


class NewMessage(Exception):
    """The user sent a message while a model call that heeds them was running (runner.call `heeds`)."""


class NeedUser(Exception):
    """An agent stopped for something only the user can give: a private code or password (`sensitive`), or
    a sign-in the user completes on the phone themselves (kind "login"). The step waits, keeps its
    session, and continues when the user answers."""

    def __init__(self, question, sensitive=False, kind=None):
        super().__init__(question)
        self.question = question
        self.sensitive = sensitive
        self.kind = kind


def safe_error(exc):
    """An error as the user and the agents may read it: never a stack trace or a request body."""
    if isinstance(exc, (ProviderError, DeviceError)):
        return str(exc)
    if isinstance(exc, (ValueError, RuntimeError)) and not isinstance(exc, httpx.HTTPError):
        return str(exc)[:500]
    return f"Execution failed ({type(exc).__name__}). Check service health and retry after inspecting the current state."
