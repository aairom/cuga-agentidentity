"""A persistent background asyncio loop for sync callers (Streamlit, CLI).

Cuga agents hold async resources (locks, stores, HTTP clients) that must stay on
one event loop; ``asyncio.run`` per call would create a new loop every time.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class AsyncRunner:
    """Owns a daemon thread running an event loop; submit coroutines from any thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="async-runner", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Coroutine[Any, Any, T], timeout: float | None = 300) -> T:
        """Run ``coro`` on the background loop and block for its result."""
        fut: Future[T] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout)

    def close(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        self._loop.close()
