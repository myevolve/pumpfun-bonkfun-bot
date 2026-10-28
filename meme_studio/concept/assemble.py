"""Turn a scored batch into one phase-0 concept document."""

from collections.abc import Iterable
from datetime import UTC, datetime

from meme_studio.concept.collisions import (
    LookupUnavailableError,
    TokenHit,
    TokenLookup,
)
from meme_studio.concept.generator import select_concept
from meme_studio.concept.schema import (
    BASE_UNKNOWNS,
    MODE,
    NETWORK_WRITES,
    PHASE,
    SCHEMA_VERSION,
    ConceptCandidate,
    ConceptDocument,
    studio_date,
)
from meme_studio.concept.scorer import ScoredBatch


class _ClosedLookup(TokenLookup):
    """Lookup used when no public list was configured."""

    def search(self, query: str) -> list[TokenHit]:
        """Fail closed.

        Args:
            query: Ignored.

        Raises:
            LookupUnavailableError: Always.
        """
        del query
        message = "token lookup is not configured"
        raise LookupUnavailableError(message)


def assemble_concept(
    batch: ScoredBatch,
    *,
    lookup: TokenLookup | None,
    history: Iterable[str],
    now: datetime,
    unknowns: Iterable[str],
) -> ConceptDocument:
    """Build the daily draft or a skipped document.

    Args:
        batch: Scored top and rejected stories.
        lookup: Public ticker lookup. None fails closed when a safe story exists.
        history: Tickers this studio already used.
        now: Aware clock. The document date is the Pacific calendar day.
        unknowns: Gap tokens to copy onto the document. Base gaps are added
            when the caller did not include them.

    Returns:
        A phase-0 document with publish and transactions left false.
    """
    history_tuple = tuple(history)
    unknown_tuple = _unknowns(unknowns)
    generated_at = now.astimezone(UTC).isoformat(timespec="seconds")
    date = studio_date(now)
    if not batch.top_stories and not batch.rejected_stories:
        return _skipped(batch, (generated_at, date, "no_sources", (), unknown_tuple))
    if not batch.top_stories:
        return _skipped(batch, (generated_at, date, "no_safe_story", (), unknown_tuple))
    active = lookup if lookup is not None else _ClosedLookup()
    chosen, candidates, reason = select_concept(
        batch.top_stories,
        lookup=active,
        history=history_tuple,
    )
    if chosen is None:
        skip_reason = reason or "all_candidates_collided"
        return _skipped(
            batch,
            (generated_at, date, skip_reason, candidates, unknown_tuple),
        )
    selected = next(item for item in candidates if item.accepted)
    return ConceptDocument(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        phase=PHASE,
        mode=MODE,
        date=date,
        status="draft",
        skipped_reason=None,
        top_stories=batch.top_stories,
        rejected_stories=batch.rejected_stories,
        selected_story_id=chosen.story_id,
        candidates=candidates,
        selected_ticker=selected.ticker,
        unknowns=unknown_tuple,
        publish=False,
        transactions=False,
        network_writes=NETWORK_WRITES,
    )


def _skipped(
    batch: ScoredBatch,
    meta: tuple[str, str, str, tuple[ConceptCandidate, ...], tuple[str, ...]],
) -> ConceptDocument:
    generated_at, date, reason, candidates, unknowns = meta
    return ConceptDocument(
        schema_version=SCHEMA_VERSION,
        generated_at=generated_at,
        phase=PHASE,
        mode=MODE,
        date=date,
        status="skipped",
        skipped_reason=reason,
        top_stories=batch.top_stories,
        rejected_stories=batch.rejected_stories,
        selected_story_id=None,
        candidates=candidates,
        selected_ticker=None,
        unknowns=unknowns,
        publish=False,
        transactions=False,
        network_writes=NETWORK_WRITES,
    )


def _unknowns(extra: Iterable[str]) -> tuple[str, ...]:
    merged: list[str] = []
    for token in (*BASE_UNKNOWNS, *tuple(extra)):
        if token not in merged:
            merged.append(token)
    return tuple(merged)
