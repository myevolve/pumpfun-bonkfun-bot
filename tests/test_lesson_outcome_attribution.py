# ruff: noqa: S101, PLR2004, TC003
"""A closed position's outcome must land on the lesson that opened it.

The journal used to attach an outcome to the newest open lesson for a mint,
so two entries in the same coin attributed the first close to the second
decision, and the score associations counted the wrong row.
"""

from __future__ import annotations

from pathlib import Path

from solders.pubkey import Pubkey

from core.pubkeys import WSOL_MINT
from learning.journal import LessonJournal, LessonObservation
from learning.report import load_report
from trading.position import Position

MINT = Pubkey.new_unique()
MINT_TEXT = str(MINT)


def _position(entry_lesson_id: int | None) -> Position:
    return Position.create_from_buy_result(
        mint=MINT,
        symbol="TOK",
        entry_price=0.5,
        quantity=1.0,
        quantity_raw=1_000,
        quote_amount_raw=10_000_000,
        buy_fee_lamports=5_000,
        position_id="signature",
        entry_lesson_id=entry_lesson_id,
    )


def test_outcome_lands_on_the_exact_entry_not_the_newest(tmp_path: Path) -> None:
    journal = LessonJournal(tmp_path / "lessons.sqlite3")
    try:
        first = journal.record(
            LessonObservation(kind="gate_pass", mint=MINT_TEXT, symbol="FIRST")
        )
        second = journal.record(
            LessonObservation(kind="gate_pass", mint=MINT_TEXT, symbol="SECOND")
        )
        assert first is not None and second is not None

        journal.link_outcome(
            MINT_TEXT,
            1_000_000,
            quote_mint=WSOL_MINT,
            reason="take_profit",
            entry_id=first,
        )
    finally:
        journal.close()

    rows = {
        row["symbol"]: row
        for row in load_report(db_path=tmp_path / "lessons.sqlite3")["recent"]
    }
    assert rows["FIRST"]["outcome_pnl_sol"] == 0.001
    assert rows["SECOND"]["outcome_pnl_sol"] is None
    assert rows["SECOND"]["outcome_source"] is None


def test_position_keeps_its_lesson_identity_across_a_restart() -> None:
    """A restart restores the journal, so the entry identity has to survive it."""
    original = _position(42)
    restored = Position.from_dict(original.to_dict())

    assert restored.entry_lesson_id == 42
    assert _position(None).entry_lesson_id is None
