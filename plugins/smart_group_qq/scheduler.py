"""Fair, coalescing workers for durable memory work; chat never waits on this queue."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SummaryRun:
    epoch: int
    future: asyncio.Future
    force_once: bool = False
    token: str = ""
    retry_once: bool = False
    reply_requested: bool = False


class MemoryScheduler:
    def __init__(self, ctx, memory, profiles, store):
        self.ctx, self.memory, self.profiles, self.store = ctx, memory, profiles, store
        self.queue = asyncio.Queue()
        self.queued = set()
        self.summaries = {}
        self.workers = []
        self.active = {}
        self.semaphore = asyncio.Semaphore(2)

    def start(self):
        if self.workers and all(not task.done() for task in self.workers):
            return
        self.workers = [task for task in self.workers if not task.done()]
        while len(self.workers) < 2:
            task = asyncio.create_task(self._worker(), name="qq-memory-worker")
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            self.workers.append(task)

    def _put(self, key):
        if key not in self.queued:
            self.queued.add(key)
            self.queue.put_nowait(key)

    def summary(self, scope, *, explicit=False, retry=False):
        """Return (completion, first subscriber). One scope owns one work item."""
        self.start()
        previous = self.summaries.get(scope)
        if previous and previous.epoch == self.store.memory_epoch(scope):
            first = explicit and not previous.reply_requested
            previous.reply_requested = previous.reply_requested or explicit
            return previous.future, first
        future = asyncio.get_running_loop().create_future()
        run = SummaryRun(self.store.memory_epoch(scope), future, explicit or retry, uuid.uuid4().hex, retry, explicit)
        self.summaries[scope] = run
        self._put(("summary", scope, run.token))
        return future, True

    def extraction(self, job_id):
        if job_id:
            self.start()
            self._put(("member", int(job_id)))

    def poll(self):
        self.start()
        for item in self.store.list_memory_backlog_groups(limit=100):
            scope = str(item["group_id"])
            if self.memory.needs_refresh(scope):
                self.summary(scope)
        for job in self.store.list_member_extraction_jobs(ready_only=True, limit=100):
            self.extraction(job["id"])

    def cancel_scope(self, scope):
        run = self.summaries.pop(scope, None)
        if run and not run.future.done():
            run.future.cancel()
        for key, (task, task_scope) in list(self.active.items()):
            if task_scope == scope and not task.done():
                task.cancel()

    async def _summary_step(self, scope, run):
        if self.summaries.get(scope) is not run:
            return False
        if run.epoch != self.store.memory_epoch(scope):
            self.cancel_scope(scope)
            return False
        before = self.memory.status(scope)
        force, run.force_once = run.force_once, False
        async with self.semaphore:
            await self.memory.refresh_ai(self.ctx, scope, force=force, max_batches=1)
        if self.summaries.get(scope) is not run:
            return False
        after = self.memory.status(scope)
        if run.retry_once and after["cursor"] == before["cursor"] and after["pending_count"]:
            self.store.pause_compaction(scope)
            after = self.memory.status(scope)
        run.retry_once = False
        # Successful batches rotate to the end; a failure yields to persisted backoff.
        more = after["cursor"] > before["cursor"] and after["pending_count"] > 0
        if not more and not run.future.done():
            run.future.set_result(after)
        return more

    async def _extract_step(self, job_id):
        job = self.store.claim_member_extraction(job_id, lease_seconds=330)
        if not job:
            return
        async with self.semaphore:
            result = await self.profiles.extract_job(self.ctx, job)
        self.store.finish_member_extraction(
            job_id, status=result.status, stored=result.stored, error=result.error,
            expected_attempt=job["attempt"],
        )

    async def _worker(self):
        while True:
            key = await self.queue.get()
            again = False
            kind, value = key[:2]
            run = self.summaries.get(value) if kind == "summary" else None
            if kind == "summary" and (run is None or run.token != key[2]):
                self.queued.discard(key)
                self.queue.task_done()
                continue
            scope = value if kind == "summary" else ""
            if kind == "member":
                row = self.store.get_member_extraction(value)
                scope = str(row["group_id"]) if row else ""
            task = asyncio.create_task(
                self._summary_step(value, run) if kind == "summary" else self._extract_step(value),
                name="qq-memory-" + kind,
            )
            self.active[key] = (task, scope)
            try:
                again = bool(await task)
            except asyncio.CancelledError:
                # Privacy cancellation cancels the child, not this shared worker.
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                logger.warning("QQ memory worker failed (%s)", type(exc).__name__)
                if kind == "summary":
                    if self.summaries.get(value) is run and not run.future.done():
                        run.future.set_result(self.memory.status(value))
                else:
                    self.store.finish_member_extraction(value, status="retryable_failure", error=type(exc).__name__)
            finally:
                self.active.pop(key, None)
                self.queued.discard(key)
                self.queue.task_done()
                if again and self.summaries.get(value) is run:
                    self._put(key)
                elif kind == "summary" and self.summaries.get(value) is run:
                    self.summaries.pop(value, None)

    async def close(self):
        for run in self.summaries.values():
            if not run.future.done():
                run.future.cancel()
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
