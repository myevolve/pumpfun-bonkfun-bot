"""Shadow CLI. ``run --dry-run`` writes concept.json and never posts."""

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from meme_studio.concept.assemble import assemble_concept
from meme_studio.concept.schema import ConceptDocument, dump_concept, validate_concept
from meme_studio.concept.scorer import score_stories
from meme_studio.concept.writer_contract import writer_packages
from meme_studio.trends.collect import attach_copycats
from meme_studio.trends.fixtures import empty_bundle, grouped_stories, load_fixtures

logger = logging.getLogger(__name__)
_DEFAULT_OUTPUT = Path("out") / "meme_studio"
_REFUSE = "phase 0 refuses to run without --dry-run"
_NO_NETWORK = "phase 0 does not open network sockets"


def main(argv: list[str] | None = None) -> int:
    """Run the shadow trend-to-concept pipeline.

    Args:
        argv: Arguments without the program name. Defaults to ``sys.argv``.

    Returns:
        0 when a concept file was written. 2 when the run is refused.
    """
    args = _parser().parse_args(argv)
    if not args.dry_run:
        print(_REFUSE, file=sys.stderr)
        return 2
    if args.allow_network:
        print(_NO_NETWORK, file=sys.stderr)
        return 2
    output = args.output if args.output is not None else _DEFAULT_OUTPUT
    now = _parse_now(args.now)
    bundle = (
        load_fixtures(args.fixtures, reddit_tos_allowed=_reddit_allowed())
        if args.fixtures is not None
        else empty_bundle()
    )
    stories = attach_copycats(grouped_stories(bundle, now=now), bundle.copycats)
    batch = score_stories(
        stories,
        now=now,
        priors=bundle.priors,
        hour_weights=bundle.hour_weights,
    )
    document = assemble_concept(
        batch,
        lookup=bundle.lookup,
        history=bundle.history,
        now=now,
        unknowns=bundle.unknowns,
    )
    validate_concept(document)
    _write(output, document)
    logger.info("shadow status=%s", document.status)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meme_studio.trends")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--output", type=Path, default=None)
    run.add_argument("--fixtures", type=Path, default=None)
    run.add_argument("--now", default=None)
    run.add_argument("--allow-network", action="store_true")
    return parser


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _reddit_allowed() -> bool:
    return os.environ.get("REDDIT_TOS_ALLOWED") == "1"


def _write(output: Path, document: ConceptDocument) -> None:
    output.mkdir(parents=True, exist_ok=True)
    text = dump_concept(document)
    (output / "concept.json").write_text(text, encoding="utf-8")
    packages = json.dumps(writer_packages(document), indent=2, sort_keys=True) + "\n"
    (output / "writer_package.json").write_text(packages, encoding="utf-8")
    cards = [card.to_dict() for card in document.top_stories]
    card_text = json.dumps(cards, indent=2, sort_keys=True) + "\n"
    (output / "story_cards.json").write_text(card_text, encoding="utf-8")
    for card in (*document.top_stories, *document.rejected_stories):
        stage = output / "runs" / document.date / card.story_id
        stage.mkdir(parents=True, exist_ok=True)
        detect = {
            "outlets": list(card.outlets),
            "sources": list(card.sources),
            "title": card.title,
        }
        detect_text = json.dumps(detect, indent=2, sort_keys=True) + "\n"
        (stage / "detect.json").write_text(detect_text, encoding="utf-8")
        score_text = json.dumps(card.to_dict(), indent=2, sort_keys=True) + "\n"
        (stage / "score.json").write_text(score_text, encoding="utf-8")
    if document.selected_story_id is not None:
        selected = output / "runs" / document.date / document.selected_story_id
        (selected / "concept.json").write_text(text, encoding="utf-8")
