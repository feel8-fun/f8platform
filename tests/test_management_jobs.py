from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from f8platform.management_jobs import ManagementJobs
from f8pysdk.management_job import ManagementJobRequest


async def no_cancel(_request: ManagementJobRequest) -> bool:
    return True


def test_fifo_failure_and_queued_cancellation_do_not_block_other_tasks(tmp_path: Path) -> None:
    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        executed: list[str | None] = []

        async def execute(request: ManagementJobRequest) -> None:
            executed.append(request.extension_id)
            if request.extension_id == 'first':
                entered.set()
                await release.wait()
                raise ValueError('Installer failed')

        queue = ManagementJobs(tmp_path, execute, no_cancel, lambda _: 'Live installer output', lambda _: True)
        first = queue.submit(ManagementJobRequest(action='install-extension', extension_id='first'))
        await entered.wait()
        second = queue.submit(ManagementJobRequest(action='uninstall-extension', extension_id='second'))
        third = queue.submit(ManagementJobRequest(action='install-extension', extension_id='third'))
        assert queue.submit(third.request).job_id == third.job_id
        assert queue.get(first.job_id).detail == 'Live installer output'
        assert queue.get(second.job_id).state == 'queued'
        await queue.cancel(second.job_id)
        release.set()
        while queue.get(third.job_id).state in {'queued', 'running'}:
            await asyncio.sleep(0)
        assert executed == ['first', 'third']
        assert queue.get(first.job_id).state == 'failed'
        assert queue.get(second.job_id).state == 'cancelled'
        assert queue.get(third.job_id).state == 'succeeded'
        assert 'ValueError: Installer failed' in (tmp_path / f'{first.job_id}.log').read_text()
        await queue.close()
        restored = ManagementJobs(tmp_path, execute, no_cancel, lambda _: '', lambda _: True)
        assert restored.get(third.job_id).state == 'succeeded'
        await restored.close()

    asyncio.run(run())


def test_running_cancellation_waits_for_installer_creation_and_returns_immediately(tmp_path: Path) -> None:
    async def run() -> None:
        entered, installer_ready, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def execute(_request: ManagementJobRequest) -> None:
            entered.set()
            await installer_ready.wait()
            await stopped.wait()

        async def cancel(_request: ManagementJobRequest) -> bool:
            if not installer_ready.is_set():
                return False
            stopped.set()
            return True

        queue = ManagementJobs(tmp_path, execute, cancel, lambda _: '', lambda _: True)
        job = queue.submit(ManagementJobRequest(action='prepare-environment', environment_id='base'))
        await entered.wait()
        requested = await queue.cancel(job.job_id)
        assert requested.state == 'running' and requested.cancel_requested
        installer_ready.set()
        await queue.close()
        assert queue.get(job.job_id).state == 'cancelled'

    asyncio.run(run())


def test_restart_reports_interrupted_tasks_without_replaying_mutations(tmp_path: Path) -> None:
    async def run() -> None:
        async def execute(_request: ManagementJobRequest) -> None:
            pytest.fail('An interrupted task must not be replayed automatically')

        queue = ManagementJobs(tmp_path, execute, no_cancel, lambda _: '', lambda _: True)
        job = queue.submit(ManagementJobRequest(action='install-extension', extension_id='extension'))
        restored = ManagementJobs(tmp_path, execute, no_cancel, lambda _: '', lambda _: True)
        assert restored.get(job.job_id).state == 'failed'
        assert 'Platform stopped' in restored.get(job.job_id).detail
        await queue.close()
        await restored.close()

    asyncio.run(run())
