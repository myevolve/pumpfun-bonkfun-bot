"""Errors raised for bad shadow-studio inputs. Nothing here posts."""

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
