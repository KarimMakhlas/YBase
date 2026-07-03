"""Formation job queue.

Documents form one at a time *per workspace* — formation links new memory into
the workspace's existing graph, so order matters within a workspace — while
independent workspaces proceed in parallel up to a global concurrency cap
(config.FORMATION_CONCURRENCY; auto = 1 on local Ollama, whose GPU queue jams
under concurrent formations, 3 on hosted providers). Failures get bounded
retries with exponential backoff; restarts can't strand documents (stuck
`processing` rows are recovered to `pending` on startup, and cancellation
re-queues the in-flight document before propagating).
"""

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core import config, db
from app.core.observability import StageTimer

log = logging.getLogger("ybase.worker")

_wake: Optional[asyncio.Event] = None
_claim_lock: Optional[asyncio.Lock] = None
_claim_lock_loop: Optional[asyncio.AbstractEventLoop] = None
_tasks: List[asyncio.Task] = []
# Timestamp of the most recent successful formation. The health endpoint uses it
# to tell a stalled queue (work pending + workers alive, nothing completing)
# apart from a healthy busy one.
_last_success_at: Optional[datetime] = None


def _mark_success() -> None:
    global _last_success_at
    _last_success_at = datetime.now(timezone.utc)


def _event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def _lock() -> asyncio.Lock:
    # On Python 3.9 a Lock binds to the loop that created it — recreate when
    # the running loop changes (each test gets a fresh loop).
    global _claim_lock, _claim_lock_loop
    loop = asyncio.get_event_loop()
    if _claim_lock is None or _claim_lock_loop is not loop:
        _claim_lock = asyncio.Lock()
        _claim_lock_loop = loop
    return _claim_lock


def _concurrency() -> int:
    if config.FORMATION_CONCURRENCY > 0:
        return config.FORMATION_CONCURRENCY
    from app.providers import llm  # lazy: llm -> config only, but keep import cycles out

    return 1 if llm.active_provider() == "ollama" else 3


async def enqueue(doc_id: int) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_status='pending', formation_error=NULL, "
            "formation_next_attempt_at=now() WHERE id=$1",
            doc_id,
        )
    _event().set()


async def recover_stuck() -> int:
    """Documents left `processing` by a crash/restart go back to `pending`."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "UPDATE documents SET formation_status='pending', formation_next_attempt_at=now() "
            "WHERE formation_status='processing' RETURNING id"
        )
    if rows:
        log.warning("recovered %d documents stuck in processing: %s",
                    len(rows), [r["id"] for r in rows])
    return len(rows)


async def queue_stats(workspace_id: Optional[int] = None) -> Dict[str, Any]:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if workspace_id is None:
            counts = await conn.fetch(
                "SELECT formation_status, count(*) AS n FROM documents GROUP BY formation_status"
            )
            last_write = await conn.fetchval("SELECT max(updated_at) FROM memory_nodes")
        else:
            counts = await conn.fetch(
                "SELECT formation_status, count(*) AS n FROM documents "
                "WHERE workspace_id=$1 GROUP BY formation_status",
                workspace_id,
            )
            last_write = await conn.fetchval(
                "SELECT max(updated_at) FROM memory_nodes WHERE workspace_id=$1",
                workspace_id,
            )
    by = {r["formation_status"]: r["n"] for r in counts}
    running = [t for t in _tasks if not t.done()]
    return {
        "pending": by.get("pending", 0),
        "processing": by.get("processing", 0),
        "complete": by.get("complete", 0),
        "failed": by.get("failed", 0),
        "last_memory_write": last_write.isoformat() if last_write else None,
        "worker_running": bool(running),
        "workers": len(running),
    }


async def formation_health() -> Dict[str, Any]:
    """Instance-wide formation health for uptime monitors: queue depth, how long
    the oldest pending document has waited, worker liveness, and seconds since
    the last success. `stalled` is true when there is pending work and live
    workers but nothing has completed recently — the signature of a wedged queue
    (vs. a healthy busy one, which keeps completing jobs)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        counts = await conn.fetch(
            "SELECT formation_status, count(*) AS n FROM documents GROUP BY formation_status"
        )
        oldest_pending = await conn.fetchval(
            "SELECT min(ingested_at) FROM documents WHERE formation_status='pending'"
        )
    by = {r["formation_status"]: r["n"] for r in counts}
    pending = by.get("pending", 0)
    now = datetime.now(timezone.utc)
    oldest_age = (now - oldest_pending).total_seconds() if oldest_pending else None
    last_success_age = (
        (now - _last_success_at).total_seconds() if _last_success_at else None
    )
    workers = len([t for t in _tasks if not t.done()])
    # Nothing has completed recently: either we've succeeded before and it was a
    # while ago, or we've never succeeded and work has been waiting too long
    # (avoids a false alarm on a freshly started instance mid-first-job).
    nothing_completing = (
        last_success_age is not None and last_success_age > config.FORMATION_STALL_S
    ) or (
        last_success_age is None
        and oldest_age is not None
        and oldest_age > config.FORMATION_STALL_S
    )
    stalled = bool(pending > 0 and workers > 0 and nothing_completing)
    return {
        "pending": pending,
        "processing": by.get("processing", 0),
        "complete": by.get("complete", 0),
        "failed": by.get("failed", 0),
        "oldest_pending_age_s": round(oldest_age) if oldest_age is not None else None,
        "last_success_age_s": round(last_success_age) if last_success_age is not None else None,
        "workers": workers,
        "stalled": stalled,
    }


async def _claim() -> Optional[int]:
    """Claim the next due document whose workspace has nothing in flight.
    The in-process lock keeps two loops from racing past the NOT EXISTS check
    into the same workspace."""
    pool = await db.get_pool()
    async with _lock():
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "UPDATE documents SET formation_status='processing' WHERE id = ("
                "  SELECT d.id FROM documents d WHERE d.formation_status='pending' "
                "  AND (d.formation_next_attempt_at IS NULL OR d.formation_next_attempt_at <= now()) "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM documents p WHERE p.workspace_id = d.workspace_id "
                "    AND p.formation_status='processing'"
                "  ) "
                "  ORDER BY d.id FOR UPDATE SKIP LOCKED LIMIT 1"
                ") RETURNING id"
            )


async def _release(doc_id: int) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_status='pending', formation_next_attempt_at=now() "
            "WHERE id=$1 AND formation_status='processing'",
            doc_id,
        )


async def _record_failure(doc_id: int, err: str) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        attempts = await conn.fetchval(
            "UPDATE documents SET formation_attempts = formation_attempts + 1 "
            "WHERE id=$1 RETURNING formation_attempts",
            doc_id,
        )
        if attempts is None:
            # the document was deleted while it was forming — nothing to record.
            # (Guards against `None >= int` killing the worker loop.)
            log.info("doc %d gone before its formation failure could be recorded", doc_id)
            return
        if attempts >= config.FORMATION_MAX_ATTEMPTS:
            await conn.execute(
                "UPDATE documents SET formation_status='failed', formation_error=$2 WHERE id=$1",
                doc_id, err[-1500:],
            )
            log.error("doc %d failed permanently after %d attempts", doc_id, attempts)
        else:
            backoff = config.FORMATION_BACKOFF_S * (2 ** (attempts - 1))
            await conn.execute(
                "UPDATE documents SET formation_status='pending', formation_error=$2, "
                "formation_next_attempt_at = now() + ($3 || ' seconds')::interval WHERE id=$1",
                doc_id, err[-1500:], str(backoff),
            )
            log.warning("doc %d attempt %d failed, retrying in %ds", doc_id, attempts, backoff)


async def _form_and_consolidate(doc_id: int, timer: StageTimer) -> None:
    """The actual formation work for one document: extract memory, then
    consolidate near-duplicate decisions in its workspace. Split out from
    _run_one so it can be wrapped in a single timeout."""
    from . import consolidate
    from .formation import run_formation

    touched = await run_formation(doc_id)
    timer.lap("formation")
    merged = []
    if touched:  # no new/updated decisions → nothing to consolidate
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            workspace_id = await conn.fetchval(
                "SELECT workspace_id FROM documents WHERE id=$1", doc_id
            )
        if workspace_id:
            merged = await consolidate.merge_similar_decisions(workspace_id, touched)
    timer.lap("consolidation")
    if merged:
        log.info("consolidation merged %d duplicate decisions", len(merged))


async def _run_one(doc_id: int) -> None:
    timer = StageTimer()
    try:
        await asyncio.wait_for(
            _form_and_consolidate(doc_id, timer),
            timeout=config.FORMATION_TASK_TIMEOUT_S,
        )
        _mark_success()
        log.info("doc %d formation complete timings %s", doc_id, timer.line())
    except asyncio.TimeoutError:
        # A hung LLM/DB call would otherwise pin this worker slot indefinitely;
        # with only a few slots shared across the whole instance, a handful of
        # hangs freeze the queue. Bound the job and record a failure so it backs
        # off and eventually lands in 'failed' rather than retrying hot forever.
        log.error("doc %d formation timed out after %ss",
                  doc_id, config.FORMATION_TASK_TIMEOUT_S)
        await _record_failure(
            doc_id, f"formation timed out after {config.FORMATION_TASK_TIMEOUT_S}s")
    except asyncio.CancelledError:
        await _release(doc_id)  # shutdown mid-formation: back to pending
        raise
    except Exception:
        timer.lap("failed")
        log.exception("doc %d formation failed timings %s", doc_id, timer.line())
        await _record_failure(doc_id, traceback.format_exc(limit=4))


async def _tick_integrations() -> None:
    from app.domains.connectors import service as sources  # lazy: slack -> ingest -> worker
    from app.domains.connectors.slack import events as slack
    from app.domains.digest import service as digest

    try:
        await slack.rollup_quiet_threads()
    except Exception:
        log.exception("slack rollup tick failed")
    try:
        await sources.resync_tick()
    except Exception:
        log.exception("source resync tick failed")
    try:
        await digest.run_digest_tick()
    except Exception:
        log.exception("digest tick failed")


async def _loop(worker_index: int) -> None:
    log.info("formation worker %d started", worker_index)
    while True:
        doc_id: Optional[int] = None
        try:
            doc_id = await _claim()
            if doc_id is None:
                if worker_index == 0:  # one ticker is enough
                    await _tick_integrations()
                try:
                    await asyncio.wait_for(_event().wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
                _event().clear()
                continue
            log.info("worker %d forming memory for document %d", worker_index, doc_id)
            await _run_one(doc_id)
            # a workspace just freed up — let sibling workers re-check the queue
            _event().set()
        except asyncio.CancelledError:
            raise  # shutdown — propagate so stop() can await us
        except Exception:
            # The loop must NEVER die: a worker that exits silently stops all
            # formation for the whole instance (observed: workers=0, queue
            # frozen). Any unexpected error is logged and the loop continues.
            log.exception("formation worker %d loop error (recovering)", worker_index)
            if doc_id is not None:
                # don't strand a claimed doc in 'processing'
                try:
                    await _release(doc_id)
                except Exception:
                    log.exception("failed to release doc %d after loop error", doc_id)
            await asyncio.sleep(1)  # avoid a hot loop if the error is persistent


def start() -> None:
    global _tasks
    _tasks = [t for t in _tasks if not t.done()]
    want = _concurrency()
    for i in range(len(_tasks), want):
        _tasks.append(asyncio.create_task(_loop(i)))


async def stop() -> None:
    global _tasks
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    _tasks = []
