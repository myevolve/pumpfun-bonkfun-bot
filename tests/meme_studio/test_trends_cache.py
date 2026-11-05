# ruff: noqa: S101
"""TTL cache for public reads. Values expire and misses return None."""

from meme_studio.trends.cache import TtlCache


def test_cache_hit_miss_and_expiry() -> None:
    clock = {"t": 0.0}
    cache = TtlCache(clock=lambda: clock["t"])
    assert cache.get("missing") is None
    cache.set("k", ["story"], ttl=5)
    assert cache.get("k") == ["story"]
    clock["t"] = 4.9
    assert cache.get("k") == ["story"]
    clock["t"] = 5.0
    assert cache.get("k") is None


def test_cache_overwrite_replaces_the_deadline() -> None:
    clock = {"t": 0.0}
    cache = TtlCache(clock=lambda: clock["t"])
    cache.set("k", "old", ttl=5)
    clock["t"] = 4.0
    cache.set("k", "new", ttl=5)
    clock["t"] = 8.0
    assert cache.get("k") == "new"
    clock["t"] = 9.0
    assert cache.get("k") is None
