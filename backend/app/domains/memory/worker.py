"""Formation job queue.

Documents form one at a time *per workspace* — formation links new memory into
the workspace's existing graph, so order matters within a workspace — while
independent workspaces proceed in parallel up to a global concurrency cap
(config.FORMATION_CONCURRENCY; auto = 1 on local Ollama, whose GPU queue jams
under concurrent formations, 3 on hosted providers). Failures get bounded
retries with exponential backoff; restarts can't strand documents (stuck
`processing` rows are recovered to `pending` on startup, and cancellation
re-queues the in-flight document before propagating).

Multi-instance: Postgres stays the source of truth (claims are FOR UPDATE
SKIP LOCKED with a per-workspace NOT EXISTS); when REDIS_URL is set,
coordination.py extends the per-workspace serialization across instances
(claim-then-lock-else-release), wakes sibling instances after an enqueue,
elects one leader for the periodic tickers, and a janitor requeues documents
stranded by a crashed instance without waiting for a restart.
"""

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional

from app.core import config, coordination, db, usage
from app.core.observability import StageTimer

log = logging.getLogger("ybase.worker")

_wake: Optional[asyncio.Event] = None
_claim_lock: Optional[asyncio.Lock] = None
_claim_lock_loop: Optional[asyncio.AbstractEventLoop] = None
_tasks: List[asyncio.Task] = []
_aux_tasks: List[asyncio.Task] = []  # wake subscriber + heartbeat (Redis mode)
# Timestamp of the most recent successful formation. The health endpoint uses it
# to tell a stalled queue (work pending + workers alive, nothing completing)
# apart from a healthy busy one.
_last_success_at: Optional[datetime] = None


class ClaimedDoc(NamedTuple):
    doc_id: int
    workspace_id: int
    lock_token: str


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
    await coordination.publish_wake()  # best-effort: sibling instances


async def recover_stuck() -> int:
    """Documents left `processing` by a crash/restart go back to `pending` —
    except those whose workspace lock is held by a live sibling instance
    (their formation is still running there, not stuck)."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, workspace_id FROM documents WHERE formation_status='processing'"
        )
        if not rows:
            return 0
        locked = await coordination.locked_workspaces({r["workspace_id"] for r in rows})
        ids = [r["id"] for r in rows if r["workspace_id"] not in locked]
        if not ids:
            return 0
        await conn.execute(
            "UPDATE documents SET formation_status='pending', "
            "formation_next_attempt_at=now(), formation_claimed_at=NULL "
            "WHERE id = ANY($1::int[]) AND formation_status='processing'",
            ids,
        )
    log.warning("recovered %d documents stuck in processing: %s", len(ids), ids)
    return len(ids)


async def janitor_tick() -> None:
    """Leader-only periodic hygiene. A crashed instance leaves its documents
    in `processing` with an expired claim; requeue them without requiring any
    instance to restart. The claim is stale once it outlives the hard task
    timeout (plus margin), and the workspace lock being absent confirms no
    live instance is still forming it. Also requeues quota-parked documents
    whose daily window has rolled over."""
    pool = await db.get_pool()
    stale_s = int(config.FORMATION_TASK_TIMEOUT_S) + 120
    async with pool.acquire() as conn:
        requeued = await conn.fetch(
            "UPDATE documents SET formation_status='pending' "
            "WHERE formation_status='rate_limited' AND formation_next_attempt_at <= now() "
            "RETURNING id"
        )
        if requeued:
            log.info("janitor requeued %d rate-limited documents", len(requeued))
            _event().set()
        rows = await conn.fetch(
            "SELECT id, workspace_id FROM documents WHERE formation_status='processing' "
            "AND formation_claimed_at IS NOT NULL "
            "AND formation_claimed_at < now() - ($1 || ' seconds')::interval",
            str(stale_s),
        )
        if not rows:
            return
        locked = await coordination.locked_workspaces({r["workspace_id"] for r in rows})
        ids = [r["id"] for r in rows if r["workspace_id"] not in locked]
        if not ids:
            return
        await conn.execute(
            "UPDATE documents SET formation_status='pending', "
            "formation_next_attempt_at=now(), formation_claimed_at=NULL "
            "WHERE id = ANY($1::int[]) AND formation_status='processing'",
            ids,
        )
    log.warning("janitor requeued %d stale processing documents: %s", len(ids), ids)


async def _janitor_consolidation() -> None:
    from . import consolidate

    await consolidate.reset_stale_runs()


async def _prune_metrics() -> None:
    """Retention for the SLO and usage tables — janitor duty, cheap via their
    time indexes."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM formation_runs WHERE started_at < now() - ($1 || ' days')::interval",
            str(config.FORMATION_RUNS_RETENTION_DAYS),
        )
        await conn.execute(
            "DELETE FROM usage_events WHERE created_at < now() - ($1 || ' days')::interval",
            str(config.USAGE_RETENTION_DAYS),
        )


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
        "rate_limited": by.get("rate_limited", 0),
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
        db_last_success = await conn.fetchval(
            "SELECT max(finished_at) FROM formation_runs WHERE status='success'"
        )
        completed_1h = await conn.fetchval(
            "SELECT count(*) FROM formation_runs WHERE status='success' "
            "AND finished_at > now() - interval '1 hour'"
        )
        failed_1h = await conn.fetchval(
            "SELECT count(*) FROM formation_runs WHERE status IN ('failed','timeout') "
            "AND finished_at > now() - interval '1 hour'"
        )
    by = {r["formation_status"]: r["n"] for r in counts}
    pending = by.get("pending", 0)
    now = datetime.now(timezone.utc)
    oldest_age = (now - oldest_pending).total_seconds() if oldest_pending else None
    # Fleet-accurate: any instance's success counts (formation_runs), not just
    # this one's in-memory timestamp. Newest wins — the module global covers
    # the window before a run row lands (or a pruned/fresh table).
    candidates = [t for t in (db_last_success, _last_success_at) if t is not None]
    last_success = max(candidates) if candidates else None
    last_success_age = (
        (now - last_success).total_seconds() if last_success else None
    )
    # Fleet-wide worker count from heartbeats when Redis coordination is on;
    # max() with the local count so a not-yet-heartbeated boot can't report 0
    # live workers while this instance clearly has some.
    local_workers = len([t for t in _tasks if not t.done()])
    fleet = await coordination.fleet_workers()
    workers = max(local_workers, fleet or 0)
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
        "rate_limited": by.get("rate_limited", 0),
        "oldest_pending_age_s": round(oldest_age) if oldest_age is not None else None,
        "last_success_age_s": round(last_success_age) if last_success_age is not None else None,
        "completed_1h": completed_1h or 0,
        "failed_1h": failed_1h or 0,
        "workers": workers,
        "stalled": stalled,
    }


async def _claim() -> Optional[ClaimedDoc]:
    """Claim the next due document whose workspace has nothing in flight.
    The in-process lock keeps two local loops from racing past the NOT EXISTS
    check into the same workspace; the Redis workspace lock (when enabled)
    extends the same guarantee across instances — two instances can both pass
    NOT EXISTS before either commit is visible, so the loser of the lock race
    hands its claim back and moves on."""
    pool = await db.get_pool()
    async with _lock():
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE documents SET formation_status='processing', "
                "formation_claimed_at=now() WHERE id = ("
                "  SELECT d.id FROM documents d WHERE d.formation_status='pending' "
                "  AND (d.formation_next_attempt_at IS NULL OR d.formation_next_attempt_at <= now()) "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM documents p WHERE p.workspace_id = d.workspace_id "
                "    AND p.formation_status='processing'"
                "  ) "
                "  AND NOT EXISTS ("  # consolidation deleting nodes mid-persist
                "    SELECT 1 FROM consolidation_queue cq WHERE cq.workspace_id = d.workspace_id "
                "    AND cq.running_since IS NOT NULL"
                "  ) "
                "  ORDER BY d.id FOR UPDATE SKIP LOCKED LIMIT 1"
                ") RETURNING id, workspace_id"
            )
    if row is None:
        return None
    token = await coordination.try_workspace_lock(row["workspace_id"])
    if token is None:
        await _release(row["id"])
        return None
    # A quota-check failure must not strand the just-claimed doc in 'processing'
    # under a held lock (janitor + TTL would eventually recover it, but only
    # after stalling the workspace for minutes). Release both and re-raise.
    try:
        over = not await _enforce_quota(row["workspace_id"])
    except Exception:
        await _release(row["id"])
        await coordination.release_workspace_lock(row["workspace_id"], token)
        raise
    if over:
        await coordination.release_workspace_lock(row["workspace_id"], token)
        return None
    return ClaimedDoc(row["id"], row["workspace_id"], token)


async def _enforce_quota(workspace_id: int) -> bool:
    """True when the fresh claim survives the workspace's daily formation
    quota. Over quota, every queued document in the workspace (including the
    one just claimed — per-workspace serialization guarantees it is the only
    'processing' row) is parked as 'rate_limited' until midnight UTC and one
    audit event records the batch. Documents are delayed, never lost; the
    janitor requeues them and this check re-parks if still over."""
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        plan = await conn.fetchval(
            "SELECT plan FROM workspaces WHERE id=$1", workspace_id
        )
        quota = config.formation_quota_for(plan)
        if quota <= 0:
            return True
        used = await conn.fetchval(
            "SELECT count(*) FROM formation_runs WHERE workspace_id=$1 "
            "AND status='success' "
            "AND started_at >= date_trunc('day', now() AT TIME ZONE 'utc') AT TIME ZONE 'utc'",
            workspace_id,
        )
        if used < quota:
            return True
        async with conn.transaction():
            rows = await conn.fetch(
                "UPDATE documents SET formation_status='rate_limited', "
                "formation_claimed_at=NULL, "
                "formation_next_attempt_at = "
                "  (date_trunc('day', now() AT TIME ZONE 'utc') + interval '1 day') AT TIME ZONE 'utc' "
                "WHERE workspace_id=$1 AND formation_status IN ('pending','processing') "
                "RETURNING id",
                workspace_id,
            )
            from app.domains.auth import service as auth  # lazy: avoid import cycle

            await auth.audit(
                conn, "formation_rate_limited", workspace_id, None,
                data={"plan": plan, "quota": quota, "used": used,
                      "count": len(rows),
                      "document_ids": [r["id"] for r in rows][:50]},
            )
    log.warning("workspace %d over daily formation quota (%d/%d) — parked %d docs "
                "until midnight UTC", workspace_id, used, quota, len(rows))
    return False


async def _release(doc_id: int) -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE documents SET formation_status='pending', "
            "formation_next_attempt_at=now(), formation_claimed_at=NULL "
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
            ws = await conn.fetchval(
                "UPDATE documents SET formation_status='failed', formation_error=$2 "
                "WHERE id=$1 RETURNING workspace_id",
                doc_id, err[-1500:],
            )
            if ws is not None:
                from app.domains.auth import service as auth  # lazy: avoid import cycle

                await auth.audit(
                    conn, "formation_failed_permanently", ws, None,
                    target_type="document", target_id=doc_id,
                    data={"attempts": attempts, "error": err[-500:]},
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


async def _form_and_consolidate(doc_id: int, timer: StageTimer) -> Dict[str, Any]:
    """The actual formation work for one document: extract memory, then queue
    its touched decisions for batch consolidation (debounced per workspace —
    ten quick ingests consolidate once, not ten times). Split out from
    _run_one so it can be wrapped in a single timeout. Returns the extraction
    validation report for the run row."""
    from . import consolidate
    from .formation import run_formation

    outcome = await run_formation(doc_id, timer)  # laps fetch / llm / persist
    if outcome.touched:  # no new/updated decisions → nothing to consolidate
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            workspace_id = await conn.fetchval(
                "SELECT workspace_id FROM documents WHERE id=$1", doc_id
            )
        if workspace_id:
            await consolidate.enqueue_touched(workspace_id, outcome.touched)
    timer.lap("consolidation")
    return outcome.validation


async def _run_meta(doc_id: int) -> Optional[Dict[str, Any]]:
    """Snapshot the run's accounting fields before formation mutates them
    (_persist resets formation_attempts on success). Queue wait = time from
    the doc becoming due (next_attempt_at, else ingest) to the claim."""
    pool = await db.get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT workspace_id, formation_attempts, ingested_at, "
                "formation_next_attempt_at, formation_claimed_at "
                "FROM documents WHERE id=$1", doc_id,
            )
    except Exception:
        log.exception("could not snapshot run meta for doc %d", doc_id)
        return None
    if row is None:
        return None
    queue_wait_ms = None
    claimed = row["formation_claimed_at"]
    ready = row["formation_next_attempt_at"] or row["ingested_at"]
    if claimed and ready:
        queue_wait_ms = max(0, int((claimed - ready).total_seconds() * 1000))
    return {
        "workspace_id": row["workspace_id"],
        "attempt": (row["formation_attempts"] or 0) + 1,
        "queue_wait_ms": queue_wait_ms,
    }


async def _record_run(
    meta: Optional[Dict[str, Any]],
    doc_id: int,
    status: str,
    timer: StageTimer,
    started_at: datetime,
    error: Optional[str] = None,
    validation: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist one formation_runs row (SLO/quota accounting). Best-effort —
    metrics must never fail or retry the job itself."""
    if meta is None:
        return
    from app.providers import llm  # lazy, mirrors _concurrency

    try:
        # Wall-clock duration, not the lap sum: a timed-out run's hung stage
        # never lapped, and its ~2ms lap total would drag P95 down instead of
        # showing the real 420s the slot was pinned.
        duration_ms = int(
            (datetime.now(timezone.utc) - started_at).total_seconds() * 1000
        )
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO formation_runs(workspace_id, document_id, status, "
                "attempt, queue_wait_ms, duration_ms, stage_timings, error, "
                "llm_provider, llm_model, started_at, finished_at, validation) "
                "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now(), $12)",
                meta["workspace_id"], doc_id, status, meta["attempt"],
                meta["queue_wait_ms"], duration_ms, timer.as_dict(),
                (error or "")[-1500:] or None,
                llm.active_provider(), llm.active_model(), started_at,
                validation or {},
            )
    except Exception:
        log.exception("failed to record formation run for doc %d", doc_id)


async def _run_one(doc_id: int) -> None:
    timer = StageTimer()
    started_at = datetime.now(timezone.utc)
    meta = await _run_meta(doc_id)
    usage_token = usage.set_context(
        workspace_id=meta["workspace_id"] if meta else None,
        surface="formation", document_id=doc_id,
    )
    try:
        validation = await asyncio.wait_for(
            _form_and_consolidate(doc_id, timer),
            timeout=config.FORMATION_TASK_TIMEOUT_S,
        )
        _mark_success()
        log.info("doc %d formation complete timings %s", doc_id, timer.line())
        await _record_run(meta, doc_id, "success", timer, started_at,
                          validation=validation)
    except asyncio.TimeoutError:
        # A hung LLM/DB call would otherwise pin this worker slot indefinitely;
        # with only a few slots shared across the whole instance, a handful of
        # hangs freeze the queue. Bound the job and record a failure so it backs
        # off and eventually lands in 'failed' rather than retrying hot forever.
        log.error("doc %d formation timed out after %ss",
                  doc_id, config.FORMATION_TASK_TIMEOUT_S)
        err = f"formation timed out after {config.FORMATION_TASK_TIMEOUT_S}s"
        await _record_failure(doc_id, err)
        await _record_run(meta, doc_id, "timeout", timer, started_at, error=err)
    except asyncio.CancelledError:
        await _release(doc_id)  # shutdown mid-formation: back to pending
        raise                   # not a terminal outcome — no run row
    except Exception:
        timer.lap("failed")
        log.exception("doc %d formation failed timings %s", doc_id, timer.line())
        err = traceback.format_exc(limit=4)
        await _record_failure(doc_id, err)
        await _record_run(meta, doc_id, "failed", timer, started_at, error=err)
    finally:
        usage.reset_context(usage_token)


async def _tick_integrations() -> None:
    from app.domains.connectors import service as sources  # lazy: slack -> ingest -> worker
    from app.domains.connectors.slack import events as slack

    try:
        await slack.rollup_quiet_threads()
    except Exception:
        log.exception("slack rollup tick failed")
    try:
        await sources.resync_tick()
    except Exception:
        log.exception("source resync tick failed")
    try:
        await janitor_tick()
    except Exception:
        log.exception("janitor tick failed")
    try:
        await _janitor_consolidation()
    except Exception:
        log.exception("consolidation janitor failed")
    try:
        await _prune_metrics()
    except Exception:
        log.exception("metrics prune failed")


async def _release_claim(claimed: ClaimedDoc) -> None:
    try:
        await coordination.release_workspace_lock(claimed.workspace_id, claimed.lock_token)
    except Exception:
        log.exception("failed to release workspace lock for %d", claimed.workspace_id)


async def _run_consolidation(ws_id: int, touched: List[int]) -> None:
    from . import consolidate

    timer = StageTimer()
    try:
        merged = await asyncio.wait_for(
            consolidate.merge_similar_decisions(ws_id, touched),
            timeout=config.CONSOLIDATION_TASK_TIMEOUT_S,
        )
        timer.lap("consolidation")
        await consolidate.finish(ws_id)
        if merged:
            log.info("batch consolidation merged %d duplicates in workspace %d %s",
                     len(merged), ws_id, timer.line())
    except asyncio.CancelledError:
        await consolidate.release(ws_id)
        raise
    except asyncio.TimeoutError:
        log.error("consolidation for workspace %d timed out after %ss",
                  ws_id, config.CONSOLIDATION_TASK_TIMEOUT_S)
        await consolidate.release(ws_id)
    except Exception:
        log.exception("consolidation for workspace %d failed", ws_id)
        await consolidate.release(ws_id)


async def _try_consolidation() -> bool:
    """Claim and run one due consolidation batch. Runs only when the loop has
    no formation to do — documents always win the slot.

    The claim shares the in-process _claim_lock with formation's _claim so the
    two claim operations serialize on a single instance: without it, a
    formation-claim and a consolidation-claim on the same workspace could both
    pass their (separate-transaction) NOT EXISTS checks against a pre-commit
    snapshot and both proceed — consolidation would then delete nodes under a
    live _persist. Across instances the Redis workspace lock below arbitrates
    the same race; the loser releases its claim."""
    from . import consolidate

    async with _lock():
        job = await consolidate.claim_due()
    if job is None:
        return False
    ws_id, touched = job
    token = await coordination.try_workspace_lock(ws_id)
    if token is None:  # a sibling instance is forming this workspace right now
        await consolidate.release(ws_id)
        return False
    try:
        await _run_consolidation(ws_id, touched)
    finally:
        await coordination.release_workspace_lock(ws_id, token)
    return True


async def _loop(worker_index: int) -> None:
    log.info("formation worker %d started", worker_index)
    while True:
        claimed: Optional[ClaimedDoc] = None
        try:
            claimed = await _claim()
            if claimed is None:
                if await _try_consolidation():
                    continue  # another batch may already be due
                # One ticker per fleet: index 0 on the leader instance.
                if worker_index == 0 and await coordination.is_leader():
                    await _tick_integrations()
                try:
                    await asyncio.wait_for(_event().wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass
                _event().clear()
                continue
            log.info("worker %d forming memory for document %d",
                     worker_index, claimed.doc_id)
            try:
                await _run_one(claimed.doc_id)
            finally:
                await _release_claim(claimed)
            # a workspace just freed up — let sibling workers re-check the queue
            _event().set()
            await coordination.publish_wake()
        except asyncio.CancelledError:
            raise  # shutdown — propagate so stop() can await us
        except Exception:
            # The loop must NEVER die: a worker that exits silently stops all
            # formation for the whole instance (observed: workers=0, queue
            # frozen). Any unexpected error is logged and the loop continues.
            log.exception("formation worker %d loop error (recovering)", worker_index)
            if claimed is not None:
                # don't strand a claimed doc in 'processing'
                try:
                    await _release(claimed.doc_id)
                except Exception:
                    log.exception("failed to release doc %d after loop error",
                                  claimed.doc_id)
                await _release_claim(claimed)
            await asyncio.sleep(1)  # avoid a hot loop if the error is persistent


async def _heartbeat_loop() -> None:
    while True:
        await coordination.heartbeat(len([t for t in _tasks if not t.done()]))
        await asyncio.sleep(10)


def start() -> None:
    global _tasks
    _tasks = [t for t in _tasks if not t.done()]
    want = _concurrency()
    for i in range(len(_tasks), want):
        _tasks.append(asyncio.create_task(_loop(i)))
    if coordination.enabled() and not [t for t in _aux_tasks if not t.done()]:
        _aux_tasks.append(coordination.subscribe_wake(lambda: _event().set()))
        _aux_tasks.append(asyncio.create_task(_heartbeat_loop()))


async def stop() -> None:
    global _tasks, _aux_tasks
    for t in _tasks + _aux_tasks:
        t.cancel()
    for t in _tasks + _aux_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    _tasks = []
    _aux_tasks = []
    # Hand the ticker lease to a surviving instance immediately, then drop
    # the Redis connection.
    await coordination.resign_leader()
    await coordination.close()
