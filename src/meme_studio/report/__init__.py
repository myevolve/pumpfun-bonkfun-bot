"""Daily 08:00 PT shadow report. Reads fixtures. Sends nothing."""

from __future__ import annotations

from meme_studio.report.contract import (
    CONTRACT_VERSION,
    PARQUET_PRODUCER_STATUS,
    validate_shadow_document,
)
from meme_studio.report.generator import (
    ReportRequest,
    generate_daily_report,
    report_due_at_0800_pt,
    run_scheduled_report,
)
from meme_studio.report.shadow import ShadowSnapshot, load_shadow_tracker

__all__ = [
    "CONTRACT_VERSION",
    "PARQUET_PRODUCER_STATUS",
    "ReportRequest",
    "ShadowSnapshot",
    "generate_daily_report",
    "load_shadow_tracker",
    "report_due_at_0800_pt",
    "run_scheduled_report",
    "validate_shadow_document",
]
