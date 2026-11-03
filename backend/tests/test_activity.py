"""Idle-aware loop pacing: fast while the app is in use, slow when idle, woken at once by a request."""
import asyncio
import time

from app import activity


def reset(monkeypatch):
    monkeypatch.setattr(activity, "_last", float("-inf"))
    monkeypatch.setattr(activity, "_event", None)


async def test_busy_app_sleeps_the_short_interval(monkeypatch):
    reset(monkeypatch)
    activity.touch()
    assert activity.busy()
    t = time.monotonic()
    await activity.nap(0.05, 5.0)
    assert time.monotonic() - t < 1.0


async def test_idle_app_naps_long_but_a_request_wakes_it_at_once(monkeypatch):
    reset(monkeypatch)
    assert not activity.busy()
    task = asyncio.create_task(activity.nap(0.05, 30.0))
    await asyncio.sleep(0.1)
    assert not task.done()                                  # still napping: nobody is using the app
    t = time.monotonic()
    activity.touch()
    await asyncio.wait_for(task, 2.0)
    assert time.monotonic() - t < 1.0


async def test_idle_nap_ends_on_its_own_timeout(monkeypatch):
    reset(monkeypatch)
    t = time.monotonic()
    await activity.nap(0.01, 0.15)
    assert 0.1 <= time.monotonic() - t < 2.0


async def test_idle_equal_to_active_is_the_old_fixed_sleep(monkeypatch):
    reset(monkeypatch)
    t = time.monotonic()
    await activity.nap(0.05, 0.05)
    assert time.monotonic() - t < 1.0
