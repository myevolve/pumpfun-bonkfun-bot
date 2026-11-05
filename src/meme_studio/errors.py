"""Raise errors that do not keep the original exception or its payload."""

from __future__ import annotations


def raise_without_context(exc: BaseException) -> None:
    """Raise ``exc`` with ``__cause__`` and ``__context__`` cleared.

    ``raise exc from None`` only hides the context. Callers that inspect the
    exception object can still read a bearer token or payload off
    ``__context__``. This helper drops both links.

    Args:
        exc: The sanitized error to propagate.

    Raises:
        BaseException: Always re-raises ``exc``.
    """
    exc.__cause__ = None
    exc.__suppress_context__ = True
    try:
        raise exc
    finally:
        exc.__context__ = None
        exc.__cause__ = None
        exc.__suppress_context__ = True
