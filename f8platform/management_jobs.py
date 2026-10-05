"""A durable FIFO queue; one maintenance operation runs at a time."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
import time
import traceback
import uuid

import msgspec

from f8pysdk.codec import copy_model
from f8pysdk.management_job import ManagementJob, ManagementJobRequest

from .errors import ConflictError, NotFoundError

logger = logging.getLogger(__name__)


class ManagementJobs:
    def __init__(self, root: Path, execute: Callable[[ManagementJobRequest], Awaitable[None]],
                 cancel: Callable[[ManagementJobRequest], Awaitable[bool]],
                 progress: Callable[[ManagementJobRequest], str],
                 cancellable: Callable[[ManagementJobRequest], bool]) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.execute = execute
        self.cancel_operation = cancel
        self.progress = progress
        self.cancellable = cancellable
        self._jobs: dict[str, ManagementJob] = {}
        self._pending: deque[str] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._cancellations: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        for path in sorted(root.glob('*.json')):
            job = msgspec.json.decode(path.read_bytes(), type=ManagementJob)
            if job.state in {'queued', 'running'}:
                job = copy_model(job, update={'state': 'failed', 'finished_at': time.time(),
                    'detail': 'Platform stopped before this task completed. Inspect the current state before retrying.',
                    'cancellable': False})
                logger.warning('Interrupted management task %s: %s', job.job_id, job.request.action)
            self._save(job)

    def _save(self, job: ManagementJob) -> None:
        temporary = self.root / f'{job.job_id}.tmp'
        temporary.write_bytes(msgspec.json.encode(job))
        temporary.replace(self.root / f'{job.job_id}.json')
        self._jobs[job.job_id] = job

    def get(self, identifier: str) -> ManagementJob:
        job = self._jobs.get(identifier)
        if job is None:
            raise NotFoundError(f'Unknown management task: {identifier}')
        if job.state == 'running':
            return copy_model(job, update={'detail': self.progress(job.request) or job.detail})
        return job

    def list(self) -> tuple[ManagementJob, ...]:
        return tuple(self.get(job.job_id) for job in sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True))

    def submit(self, request: ManagementJobRequest) -> ManagementJob:
        if self._closing:
            raise ConflictError('Platform is shutting down')
        existing = next((job for job in self._jobs.values()
                         if job.state in {'queued', 'running'} and job.request == request), None)
        if existing is not None:
            return self.get(existing.job_id)
        job = ManagementJob(job_id=uuid.uuid4().hex, request=request, state='queued', created_at=time.time(),
                            detail='Waiting in the maintenance queue')
        self._save(job)
        self._pending.append(job.job_id)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name='platform-maintenance-queue')
            self._worker.add_done_callback(self._report_failure)
        return job

    @staticmethod
    def _report_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            logger.error('Maintenance queue worker stopped unexpectedly',
                         exc_info=(type(failure), failure, failure.__traceback__))

    async def _run(self) -> None:
        while self._pending:
            identifier = self._pending.popleft()
            job = self._jobs[identifier]
            if job.state != 'queued':
                continue
            self._save(copy_model(job, update={'state': 'running', 'started_at': time.time(),
                'detail': 'Executing task', 'cancellable': False}))
            try:
                self._save(copy_model(self._jobs[identifier], update={'cancellable': self.cancellable(job.request)}))
                await self.execute(job.request)
            except Exception as exc:
                # The worker is the task execution boundary: report and continue the queue.
                logger.exception('Management task %s failed (%s)', identifier, job.request.action)
                (self.root / f'{identifier}.log').write_text(traceback.format_exc(), encoding='utf-8')
                current = self._jobs[identifier]
                self._save(copy_model(current, update={'state': 'cancelled' if current.cancel_requested else 'failed',
                    'detail': f'{type(exc).__name__}: {exc}', 'finished_at': time.time(), 'cancellable': False}))
            else:
                current = self._jobs[identifier]
                self._save(copy_model(current, update={'state': 'cancelled' if current.cancel_requested else 'succeeded',
                    'detail': 'Task cancelled' if current.cancel_requested else 'Task completed',
                    'finished_at': time.time(), 'cancellable': False}))

    async def cancel(self, identifier: str) -> ManagementJob:
        job = self.get(identifier)
        if job.state not in {'queued', 'running'}:
            return job
        if not job.cancellable:
            raise ConflictError('This task has already started a phase that cannot be cancelled safely')
        if job.state == 'queued':
            self._save(copy_model(job, update={'state': 'cancelled', 'cancel_requested': True,
                'finished_at': time.time(), 'detail': 'Cancelled before execution', 'cancellable': False}))
        else:
            self._save(copy_model(job, update={'cancel_requested': True}))
            if identifier not in self._cancellations:
                self._cancellations[identifier] = asyncio.create_task(self._cancel_running(identifier), name=f'cancel-maintenance:{identifier}')
        return self.get(identifier)

    async def _cancel_running(self, identifier: str) -> None:
        try:
            while self._jobs[identifier].state == 'running':
                if await self.cancel_operation(self._jobs[identifier].request):
                    return
                # Submission may be cancelled before its installer subprocess is created.
                await asyncio.sleep(0.05)
        except Exception:
            logger.exception('Cannot cancel management task %s', identifier)
            current = self._jobs[identifier]
            self._save(copy_model(current, update={'cancel_requested': False,
                'detail': 'Cancellation failed; see the platform console log.'}))

    async def close(self) -> None:
        self._closing = True
        for job in tuple(self._jobs.values()):
            if job.state == 'queued' or (job.state == 'running' and job.cancellable):
                await self.cancel(job.job_id)
        if self._worker is not None:
            await self._worker
        if self._cancellations:
            await asyncio.gather(*self._cancellations.values())
