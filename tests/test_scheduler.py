"""Scheduler: failure isolation, backoff and recovery."""
import asyncio

from backend.core.scheduler import Scheduler


async def test_failing_task_does_not_stop_others():
    sch = Scheduler()
    ok = []
    state = {"n": 0}

    async def bad():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("source down")

    async def good():
        ok.append(1)
    t_bad = sch.add("bad", bad, 0.01, jitter=0)
    t_bad.max_backoff = 0.05
    sch.add("good", good, 0.01, jitter=0)
    sch.start()
    await asyncio.sleep(0.6)
    await sch.stop()
    assert len(ok) > 5
    assert t_bad.failures == 2 and t_bad.consecutive_failures == 0 and t_bad.last_error is None  # recovered


async def test_dynamic_interval():
    sch = Scheduler()
    mode = {"live": False}
    t = sch.add("x", lambda: asyncio.sleep(0), lambda: 10 if mode["live"] else 120)
    assert t.current_interval() == 120
    mode["live"] = True
    assert t.current_interval() == 10
