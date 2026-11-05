"""Write timestamped launch-day drafts to disk. Never posts them."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from meme_studio.compliance.lint import compliance_lint
from meme_studio.compliance.models import LintContext, Post
from meme_studio.drafts.models import DraftRecord
from meme_studio.drafts.schedule import INTERNAL, SLOTS
from meme_studio.drafts.templates import render_sell, render_template
from meme_studio.errors import NaiveTimeError, UnknownDraftModeError

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from meme_studio.drafts.models import ConceptPackage, SellEvent
    from meme_studio.drafts.schedule import Slot

_X_HISTORY = 50


@dataclass
class _Outbox:
    """Mutable write state for one launch-day folder."""

    concept: ConceptPackage
    t0: datetime
    output_dir: Path
    history: list[str]


@dataclass(frozen=True)
class _DraftSpec:
    """Fields for one JSON draft."""

    template_id: str
    channel: str
    target: str
    offset: str
    scheduled_at: datetime
    text: str
    index: int
    reply: bool
    trend_ref: bool
    generated: bool


def write_launch_drafts(
    concept: ConceptPackage,
    t0: datetime,
    output_dir: Path,
    *,
    mode: str = "full",
) -> list[DraftRecord]:
    """Render the T-30m..T+24h schedule into JSON drafts under ``output_dir``.

    Each draft is linted first. Files are the only output. ``mode`` ``recap_only``
    drops the alive-audience posts and keeps the 24h recap.
    """
    if t0.tzinfo is None:
        raise NaiveTimeError
    if mode not in {"full", "recap_only"}:
        raise UnknownDraftModeError
    for sell in concept.sells:
        if sell.at.tzinfo is None:
            raise NaiveTimeError
    output_dir.mkdir(parents=True, exist_ok=True)
    outbox = _Outbox(concept=concept, t0=t0, output_dir=output_dir, history=[])
    records: list[DraftRecord] = []
    for index, slot in enumerate(SLOTS):
        if slot.gate == "alive" and mode == "recap_only":
            continue
        records.append(_write_slot(outbox, slot, index))
    for sell in concept.sells:
        records.append(_write_sell(outbox, sell, len(records)))
    _write_manifest(output_dir, t0, mode, records)
    return records


def _write_slot(outbox: _Outbox, slot: Slot, index: int) -> DraftRecord:
    scheduled_at = outbox.t0 + timedelta(seconds=slot.delta_seconds)
    text = render_template(
        slot.template_id,
        outbox.concept,
        slot.variant,
        as_of=scheduled_at,
    )
    spec = _DraftSpec(
        template_id=slot.template_id,
        channel=slot.channel,
        target=slot.target,
        offset=slot.offset,
        scheduled_at=scheduled_at,
        text=text,
        index=index,
        reply=slot.reply,
        trend_ref=slot.trend_ref,
        generated=slot.generated,
    )
    return _store(outbox, spec)


def _write_sell(outbox: _Outbox, sell: SellEvent, index: int) -> DraftRecord:
    spec = _DraftSpec(
        template_id="P2",
        channel="x",
        target="",
        offset=f"sell-{sell.n}",
        scheduled_at=sell.at,
        text=render_sell(outbox.concept, sell),
        index=index,
        reply=False,
        trend_ref=False,
        generated=False,
    )
    return _store(outbox, spec)


def _store(outbox: _Outbox, spec: _DraftSpec) -> DraftRecord:
    concept = outbox.concept
    post = Post(
        text=spec.text,
        channel=spec.channel,
        template_id=spec.template_id,
        is_reply=spec.reply,
        trend_ref=spec.trend_ref,
        uses_generated_media=spec.generated,
        ticker=concept.ticker,
        mint=concept.mint,
        registered_wallets=(concept.wallet, concept.mint),
    )
    lint = compliance_lint(post, _context(spec.channel, outbox.history))
    if spec.channel == "x":
        outbox.history.append(spec.text)
    filename = _filename(spec)
    path = outbox.output_dir / filename
    payload = {
        "template_id": spec.template_id,
        "channel": spec.channel,
        "target": spec.target,
        "offset": spec.offset,
        "scheduled_at": spec.scheduled_at.isoformat(),
        "text": spec.text,
        "is_reply": spec.reply,
        "trend_ref": spec.trend_ref,
        "uses_generated_media": spec.generated,
        "lint": {
            "status": lint.status,
            "passed": lint.passed,
            "reasons": list(lint.reasons),
            "human_approval_required": lint.human_approval_required,
            "payload_hash": lint.payload_hash,
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return DraftRecord(
        template_id=spec.template_id,
        channel=spec.channel,
        offset=spec.offset,
        scheduled_at=spec.scheduled_at,
        text=spec.text,
        path=path,
        lint=lint,
        is_reply=spec.reply,
        human_approval_required=lint.human_approval_required,
    )


def _context(channel: str, history: list[str]) -> LintContext:
    if channel != "x":
        return LintContext()
    return LintContext(history=tuple(history[-_X_HISTORY:]))


def _filename(spec: _DraftSpec) -> str:
    target_bit = f"__{spec.target}" if spec.target else ""
    return f"{spec.offset}__{spec.channel}{target_bit}__{spec.template_id}__{spec.index}.json"


def _write_manifest(
    output_dir: Path,
    t0: datetime,
    mode: str,
    records: list[DraftRecord],
) -> None:
    schedule: list[dict[str, object]] = [
        {"offset": offset, "channel": "internal", "action": action, "draft": None}
        for offset, action in INTERNAL
    ]
    posts: list[dict[str, object]] = []
    for record in records:
        schedule.append(
            {
                "offset": record.offset,
                "channel": record.channel,
                "action": record.template_id,
                "draft": record.path.name,
            }
        )
        posts.append(
            {
                "template_id": record.template_id,
                "channel": record.channel,
                "offset": record.offset,
                "path": record.path.name,
                "human_approval_required": record.human_approval_required,
                "lint": {"status": record.lint.status},
            }
        )
    manifest = {
        "phase": "shadow",
        "t0": t0.isoformat(),
        "mode": mode,
        "posts": posts,
        "schedule": schedule,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
