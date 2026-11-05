"""In-memory TTL cache for public reads."""

from collections.abc import Callable


class TtlCache:
    """Store values until a monotonic deadline supplied by ``clock``."""

    def __init__(self, *, clock: Callable[[], float]) -> None:
        """Create an empty cache.

        Args:
            clock: Returns the current time in seconds. Tests inject this.
        """
        self._clock = clock
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        """Return a live value, or None when the key is missing or expired.

        Args:
            key: Cache key. Callers must not put secrets in the key.

        Returns:
            The stored value, or None on a miss. None itself cannot be stored.
        """
        item = self._store.get(key)
        if item is None:
            return None
        expires_at, value = item
        if self._clock() >= expires_at:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: object, *, ttl: float) -> None:
        """Store ``value`` until ``ttl`` seconds after the current clock.

        Args:
            key: Cache key.
            value: JSON-like or dataclass value. Do not store request headers.
            ttl: Positive lifetime in seconds.

        Raises:
            ValueError: When ``ttl`` is not positive.
        """
        if ttl <= 0:
            message = "ttl must be positive"
            raise ValueError(message)
        self._store[key] = (self._clock() + ttl, value)
