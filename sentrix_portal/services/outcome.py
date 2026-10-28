"""The result of a Portal flow (sign-in, a wizard step, a settings section...),
shared by the HTML routes and the JSON API (``routes/api_v2``).

A flow decides and records; it never renders. ``status`` is what the HTML
route answers (its pages keep their old status codes); ``code`` is the JSON
API's error code (docs/9 §4: ``validation``, ``step_up_required``,
``rate_limited``, ``conflict``, ``not_found``, ...), ``message`` the plain
English the page showed, ``errors`` the field messages of a form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sentrix_portal.services.accounts import FormErrors


@dataclass
class Outcome:
    ok: bool = True
    status: int = 200
    code: str = "ok"
    message: str | None = None
    errors: FormErrors = field(default_factory=FormErrors)
    retry_after: int = 0
    data: dict[str, Any] = field(default_factory=dict)


def done(message: str | None = None, **data: Any) -> Outcome:
    return Outcome(message=message, data=data)


def failed(code: str, message: str | None = None, *, status: int = 400,
           errors: FormErrors | dict | None = None, retry_after: int = 0,
           **data: Any) -> Outcome:
    found = FormErrors()
    found.update(errors or {})
    return Outcome(False, status, code, message, found, retry_after, data)


def invalid(errors: FormErrors, *, status: int = 400, message: str | None = None,
            step_up_field: str | None = None, **data: Any) -> Outcome:
    """A form with field errors. When the only error is the missing password
    confirmation (``step_up_field``), the API answers ``step_up_required``."""
    code = ("step_up_required" if step_up_field and set(errors) == {step_up_field}
            else "validation")
    return failed(code, message or next(iter(errors.values()), None), status=status,
                  errors=errors, **data)


__all__ = ["Outcome", "done", "failed", "invalid"]
