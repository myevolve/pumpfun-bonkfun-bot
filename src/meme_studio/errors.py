"""Errors for bad shadow-studio inputs, plus context-free re-raising. Nothing here posts."""

from __future__ import annotations


class StudioInputError(ValueError):
    """The caller passed a value this phase cannot accept."""


class UnknownMediaKindError(StudioInputError):
    """Image and video are the only media kinds."""

    def __init__(self) -> None:
        super().__init__("unknown media kind")


class NaiveTimeError(StudioInputError):
    """Timestamps in this package must carry a timezone."""

    def __init__(self) -> None:
        super().__init__("naive datetime")


class UnknownDraftModeError(StudioInputError):
    """Draft mode must be full or recap_only."""

    def __init__(self) -> None:
        super().__init__("unknown draft mode")


class UnsafeStemError(StudioInputError):
    """An asset stem would escape the output directory or collide with a path."""

    def __init__(self) -> None:
        super().__init__("unsafe asset stem")


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
