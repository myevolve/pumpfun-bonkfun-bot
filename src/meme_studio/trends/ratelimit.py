"""Fixed-window limiter shared by every public read."""

import time
from collections.abc import Callable


class RateLimiter:
    """Allow ``max_calls`` acquires inside each ``per_seconds`` window."""

    def __init__(
        self,
        *,
        max_calls: int,
        per_seconds: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Create a limiter.

        Args:
            max_calls: Positive cap inside one window.
            per_seconds: Positive window length.
            clock: Current time in seconds.
            sleep: Wait function. Defaults to ``time.sleep``. Tests advance a
                fake clock from this callback.

        Raises:
            ValueError: When the cap or the window is not positive.
        """
        if max_calls < 1:
            message = "max_calls must be positive"
            raise ValueError(message)
        if per_seconds <= 0:
            message = "per_seconds must be positive"
            raise ValueError(message)
        self._max_calls = max_calls
        self._per_seconds = per_seconds
        self._clock = clock
        self._sleep = time.sleep if sleep is None else sleep
        self._hits: list[float] = []

    def acquire(self) -> None:
        """Block until a slot is free, then record one call."""
        while True:
            now = self._clock()
            window_start = now - self._per_seconds
            self._hits = [hit for hit in self._hits if hit > window_start]
            if len(self._hits) < self._max_calls:
                self._hits.append(now)
                return
            wait = self._hits[0] + self._per_seconds - now
            if wait > 0:
                self._sleep(wait)
            else:
                self._sleep(self._per_seconds)
