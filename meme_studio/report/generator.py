"""Build the 08:00 PT shadow report as markdown and JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from meme_studio.errors import NaiveTimeError
from meme_studio.report.shadow import ShadowSnapshot, load_shadow_tracker

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

_PT = ZoneInfo("America/Los_Angeles")
_DUE_HOUR = 8
_DUE_MINUTE = 0
_PRIORS = "No prior update: shadow sample is below 30 launches."


@dataclass(frozen=True)
class ReportRequest:
    """Inputs for one daily report. All of them are local files or dicts."""

    output_dir: Path
    concept: dict[str, object]
    compliance_results: tuple[dict[str, object], ...]
    drafts_dir: Path
    shadow_path: Path
    now: datetime
    incidents: tuple[str, ...] = ()
    pnl_label: str = "PAPER"
    art_dir: Path | None = None


def report_due_at_0800_pt(now: datetime) -> bool:
    """Return True only during the 08:00 minute in America/Los_Angeles."""
    if now.tzinfo is None:
        raise NaiveTimeError
    local = now.astimezone(_PT)
    return local.hour == _DUE_HOUR and local.minute == _DUE_MINUTE


def generate_daily_report(request: ReportRequest) -> dict[str, object]:
    """Write ``report.md`` and ``report.json`` for a shadow day.

    The clock does not have to be 08:00. ``run_scheduled_report`` is the gate.
    P&L stays labelled with the request's label, which defaults to PAPER.
    """
    snapshot = load_shadow_tracker(request.shadow_path)
    body: dict[str, object] = {
        "phase": "shadow",
        "schedule": "08:00 PT",
        "generated_at": request.now.isoformat(),
        "due": report_due_at_0800_pt(request.now),
        "pnl_label": request.pnl_label,
        "concept": request.concept,
        "stories": list(request.concept.get("stories_seen", [])),
        "compliance": _compliance(request.compliance_results),
        "drafts": _drafts(request.drafts_dir),
        "shadow": _shadow_body(snapshot),
        "plan_adherence": _adherence(snapshot),
        "incidents": list(request.incidents),
        "priors": _PRIORS,
        "art": _art(request.art_dir),
    }
    request.output_dir.mkdir(parents=True, exist_ok=True)
    (request.output_dir / "report.json").write_text(
        json.dumps(body, indent=2) + "\n",
        encoding="utf-8",
    )
    (request.output_dir / "report.md").write_text(_markdown(body), encoding="utf-8")
    return body


def run_scheduled_report(request: ReportRequest) -> dict[str, object] | None:
    """Write the report only when the Pacific clock is 08:00."""
    if not report_due_at_0800_pt(request.now):
        return None
    return generate_daily_report(request)


def _compliance(results: tuple[dict[str, object], ...]) -> dict[str, object]:
    passed = sum(1 for item in results if item.get("status") == "pass")
    return {
        "results": [dict(item) for item in results],
        "pass": passed,
        "fail": len(results) - passed,
    }


def _drafts(drafts_dir: Path) -> dict[str, object]:
    manifest_path = drafts_dir / "manifest.json"
    if not manifest_path.is_file():
        return {"posts": [], "channels": [], "human_approval_required": []}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    posts = manifest.get("posts", [])
    if not isinstance(posts, list):
        posts = []
    channels = sorted(
        {str(post.get("channel", "")) for post in posts if isinstance(post, dict)}
    )
    human = [
        str(post.get("template_id", ""))
        for post in posts
        if isinstance(post, dict) and post.get("human_approval_required")
    ]
    return {
        "posts": posts,
        "channels": channels,
        "human_approval_required": human,
    }


def _art(art_dir: Path | None) -> list[dict[str, object]]:
    if art_dir is None or not art_dir.is_dir():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(art_dir.glob("*.meta.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _shadow_body(snapshot: ShadowSnapshot) -> dict[str, object]:
    mints = [
        {
            "mint": item.mint,
            "symbol": item.symbol,
            "creator": item.creator,
            "outside_buyers": item.outside_buyers,
            "creator_fees_sol": item.creator_fees_sol,
            "graduated": item.graduated,
            "dev_buy_sol": item.dev_buy_sol,
            "mayhem": item.mayhem,
            "story_id": item.story_id,
        }
        for item in snapshot.mints
    ]
    return {
        "date": snapshot.date,
        "mints": mints,
        "paper_replay": snapshot.paper_replay,
        "random_control": snapshot.random_control,
        "story_ids": list(snapshot.story_ids),
    }


def _adherence(snapshot: ShadowSnapshot) -> object:
    sized = snapshot.paper_replay.get("0.1")
    if isinstance(sized, dict) and "plan_adherence" in sized:
        return sized["plan_adherence"]
    return None


def _markdown(body: dict[str, object]) -> str:
    lines = [
        "# Daily shadow report",
        "",
        "Phase: shadow",
        "Schedule: 08:00 PT",
        f"P&L label: {body['pnl_label']}",
        f"Plan adherence: {body['plan_adherence']}",
        "",
        "## Concept",
        _concept_line(body["concept"]),
    ]
    lines.extend(_story_lines(body.get("stories")))
    lines.extend(_compliance_lines(body.get("compliance")))
    lines.extend(_draft_lines(body.get("drafts")))
    lines.extend(_shadow_lines(body.get("shadow")))
    lines.extend(_art_lines(body.get("art")))
    lines.extend(_incident_lines(body.get("incidents")))
    lines.extend(["", "## Priors", str(body["priors"]), ""])
    return "\n".join(lines)


def _story_lines(stories: object) -> list[str]:
    lines = ["", "## Stories"]
    if isinstance(stories, list):
        lines.extend(_story_item(story) for story in stories if isinstance(story, dict))
    return lines


def _story_item(story: dict[str, object]) -> str:
    return (
        f"- {story.get('id')} score={story.get('score')} {story.get('skip_reason', '')}"
    )


def _compliance_lines(compliance: object) -> list[str]:
    lines = ["", "## Compliance"]
    if not isinstance(compliance, dict):
        return lines
    results = compliance.get("results", [])
    if not isinstance(results, list):
        return lines
    for result in results:
        lines.extend(_reason_lines(result))
    return lines


def _reason_lines(result: object) -> list[str]:
    if not isinstance(result, dict):
        return []
    reasons = result.get("reasons", [])
    if not isinstance(reasons, list):
        return []
    return [" ".join(str(reason) for reason in reasons)]


def _draft_lines(drafts: object) -> list[str]:
    lines = ["", "## Drafts"]
    if not isinstance(drafts, dict):
        return lines
    channels = drafts.get("channels", [])
    human = drafts.get("human_approval_required", [])
    lines.append("channels: " + ", ".join(str(item) for item in channels))
    lines.append("human approval: " + ", ".join(str(item) for item in human))
    return lines


def _shadow_lines(shadow: object) -> list[str]:
    lines = ["", "## Shadow tracker"]
    if not isinstance(shadow, dict):
        return lines
    mints = shadow.get("mints", [])
    if not isinstance(mints, list):
        return lines
    for mint in mints:
        if isinstance(mint, dict):
            lines.append(_mint_line(mint))
    return lines


def _mint_line(mint: dict[str, object]) -> str:
    return (
        f"- {mint.get('symbol')} outside_buyers={mint.get('outside_buyers')} "
        f"fees={mint.get('creator_fees_sol')} graduated={mint.get('graduated')}"
    )


def _art_lines(art: object) -> list[str]:
    lines = ["", "## Generated media"]
    if not isinstance(art, list):
        return lines
    for item in art:
        if isinstance(item, dict):
            lines.append(
                f"- {item.get('provider')} {item.get('model')} {item.get('timestamp')}"
            )
    return lines


def _incident_lines(incidents: object) -> list[str]:
    lines = ["", "## Incidents"]
    if not isinstance(incidents, list):
        return lines
    lines.extend(f"- {incident}" for incident in incidents)
    return lines


def _concept_line(concept: object) -> str:
    if not isinstance(concept, dict):
        return "No concept."
    chosen = concept.get("chosen")
    reason = concept.get("skip_reason") or "selected"
    return f"{concept.get('name')} ({concept.get('ticker')}) chosen={chosen} {reason}"
