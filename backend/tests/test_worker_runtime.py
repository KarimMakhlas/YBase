"""Runtime-role contracts for separated worker task groups."""

import asyncio

from app import main
from app.domains.memory import worker


async def test_api_runtime_role_does_not_start_background_task_groups(monkeypatch):
    calls = []

    async def note(name):
        calls.append(name)

    monkeypatch.setattr(main.config, "RUNTIME_ROLE", "api")
    monkeypatch.setattr(main.migrate, "run", lambda: note("migrate"))
    monkeypatch.setattr(main, "_warn_on_missing_email_provider", lambda: calls.append("warn"))
    monkeypatch.setattr(main.sources, "recover_stuck_sync_jobs", lambda: note("recover-sources"))
    monkeypatch.setattr(main.worker, "recover_stuck", lambda: note("recover-worker"))
    monkeypatch.setattr(main.worker, "start_formation", lambda: calls.append("formation"))
    monkeypatch.setattr(main.worker, "start_periodic", lambda: calls.append("periodic"))
    monkeypatch.setattr(main.worker, "stop", lambda: note("stop"))
    monkeypatch.setattr(main.sources, "stop_sync_tasks", lambda: note("stop-sources"))
    monkeypatch.setattr(main.db, "close_pool", lambda: note("close-db"))

    async with main.lifespan(main.app):
        assert calls == ["migrate", "warn"]

    assert calls == ["migrate", "warn", "close-db"]


async def test_worker_runtime_role_starts_separate_task_groups(monkeypatch):
    calls = []

    async def note(name):
        calls.append(name)

    monkeypatch.setattr(main.config, "RUNTIME_ROLE", "worker")
    monkeypatch.setattr(main.migrate, "run", lambda: note("migrate"))
    monkeypatch.setattr(main, "_warn_on_missing_email_provider", lambda: calls.append("warn"))
    monkeypatch.setattr(main.sources, "recover_stuck_sync_jobs", lambda: note("recover-sources"))
    monkeypatch.setattr(main.worker, "recover_stuck", lambda: note("recover-worker"))
    monkeypatch.setattr(main.worker, "start_formation", lambda: calls.append("formation"))
    monkeypatch.setattr(main.worker, "start_periodic", lambda: calls.append("periodic"))
    monkeypatch.setattr(main.worker, "stop", lambda: note("stop"))
    monkeypatch.setattr(main.sources, "stop_sync_tasks", lambda: note("stop-sources"))
    monkeypatch.setattr(main.db, "close_pool", lambda: note("close-db"))

    async with main.lifespan(main.app):
        assert calls == ["migrate", "warn", "recover-sources", "recover-worker", "formation", "periodic"]

    assert calls == [
        "migrate", "warn", "recover-sources", "recover-worker", "formation", "periodic",
        "stop-sources", "stop", "close-db",
    ]


async def test_integration_tick_runs_without_waiting_for_a_formation_claim(monkeypatch):
    ticked = asyncio.Event()

    async def leader():
        return True

    async def tick():
        ticked.set()

    monkeypatch.setattr(worker.coordination, "is_leader", leader)
    monkeypatch.setattr(worker, "_tick_integrations", tick)
    monkeypatch.setattr(worker.config, "WORKER_PERIODIC_TICK_S", 3600)
    task = asyncio.create_task(worker._integration_loop(0))
    try:
        await asyncio.wait_for(ticked.wait(), timeout=1)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
