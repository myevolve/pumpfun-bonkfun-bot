"""Raw public-source rows before they are grouped into stories."""

from dataclasses import dataclass
from datetime import datetime

from meme_studio.concept.inputs import CountPoint


@dataclass(frozen=True)
class RawItem:
    """One headline from a public source."""

    source: str
    title: str
    summary: str
    published_at: datetime
    outlet: str
    url: str


@dataclass(frozen=True)
class TrendSignal:
    """An X trend name plus optional post-count buckets."""

    name: str
    post_counts: tuple[CountPoint, ...]
