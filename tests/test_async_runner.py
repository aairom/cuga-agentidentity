import asyncio
import threading

import pytest

from agent_identity.async_runner import AsyncRunner


def test_runs_coroutines_on_one_persistent_loop():
    r = AsyncRunner()
    try:
        async def loop_id():
            return id(asyncio.get_running_loop()), threading.current_thread().name

        a, b = r.run(loop_id()), r.run(loop_id())
        assert a == b and a[1] == "async-runner"
    finally:
        r.close()


def test_propagates_exceptions_and_timeouts():
    r = AsyncRunner()
    try:
        async def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            r.run(boom())
        with pytest.raises(Exception):
            r.run(asyncio.sleep(2), timeout=0.05)
    finally:
        r.close()
