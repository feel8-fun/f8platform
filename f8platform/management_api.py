"""Extension, runtime and tool APIs hosted by the platform daemon."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeVar

import msgspec
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse
from f8pysdk.specs import F8JsonValue
from f8pysdk.extension_status import (
    ExtensionImportRequest, ExtensionToggleRequest, RuntimeStorageRequest,
)
from f8pysdk.platform_spec import ServiceStartRequest
from f8pysdk.tool_spec import ToolRunRequest
from f8pysdk.management_job import ManagementJobRequest, ManagementJobsClearRequest
from .runtime import PlatformRuntime

T = TypeVar('T')


def _json_value(value: object) -> F8JsonValue:
    return msgspec.json.decode(msgspec.json.encode(value), type=F8JsonValue)


async def decode_body(request: Request, model: type[T]) -> T:
    try:
        return msgspec.json.decode(await request.body(), type=model)
    except msgspec.DecodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def install_management_routes(app: FastAPI, get_runtime: Callable[[], PlatformRuntime]) -> None:
    @app.get('/api/management-jobs')
    async def management_jobs() -> F8JsonValue:
        return _json_value(get_runtime().jobs.list())

    @app.post('/api/management-jobs', status_code=202)
    async def submit_management_job(request: Request) -> F8JsonValue:
        return _json_value(get_runtime().submit_job(await decode_body(request, ManagementJobRequest)))

    @app.post('/api/management-jobs/clear-completed')
    async def clear_completed_management_jobs(request: Request) -> F8JsonValue:
        payload = await decode_body(request, ManagementJobsClearRequest)
        return _json_value(get_runtime().jobs.clear_completed(payload.job_ids))

    @app.get('/api/management-jobs/{job_id}')
    async def management_job(job_id: str) -> F8JsonValue:
        return _json_value(get_runtime().jobs.get(job_id))

    @app.get('/api/management-jobs/{job_id}/logs')
    async def management_job_logs(job_id: str) -> F8JsonValue:
        return _json_value(await asyncio.to_thread(get_runtime().job_log, job_id))

    @app.post('/api/management-jobs/{job_id}/cancel', status_code=202)
    async def cancel_management_job(job_id: str) -> F8JsonValue:
        return _json_value(await get_runtime().jobs.cancel(job_id))

    @app.get('/api/extension-tools')
    async def extension_tools() -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.tools.list())

    @app.post('/api/extension-tools/{extension_id}/{tool_id}/run', status_code=202)
    async def run_extension_tool(extension_id: str, tool_id: str, request: Request) -> F8JsonValue:
        runtime = get_runtime()
        payload = await decode_body(request, ToolRunRequest)
        return _json_value(runtime.tools.submit(extension_id, tool_id, payload))

    @app.get('/api/tool-jobs')
    async def tool_jobs() -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.tools.jobs())

    @app.get('/api/tool-jobs/{job_id}')
    async def tool_job(job_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.tools.get(job_id))

    @app.post('/api/tool-jobs/{job_id}/cancel')
    async def cancel_tool_job(job_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await runtime.tools.cancel(job_id))

    @app.get('/api/extension-resources')
    async def extension_resources() -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.tools.resources())

    @app.get('/api/extension-resources/{extension_id}/{resource_id}/file')
    async def download_extension_resource(extension_id: str, resource_id: str) -> FileResponse:
        runtime = get_runtime()
        path = runtime.tools.resource_path(extension_id, resource_id)
        return FileResponse(path, filename=path.name)

    @app.get('/api/extension-resources/{extension_id}/{resource_id}')
    async def read_extension_resource(extension_id: str, resource_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await asyncio.to_thread(runtime.tools.read_resource, extension_id, resource_id))

    @app.get('/api/extensions')
    async def extensions() -> F8JsonValue:
        runtime = get_runtime()
        await runtime.development.probe_observed()
        return _json_value(runtime.statuses())

    @app.post('/api/extensions/import', status_code=202)
    async def import_extension_package(request: Request) -> F8JsonValue:
        runtime = get_runtime()
        payload = await decode_body(request, ExtensionImportRequest)
        return _json_value(runtime.submit_job(ManagementJobRequest(action='import-extension', package=payload)))

    @app.get('/api/extensions/{extension_id}/detail')
    async def extension_detail(extension_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await asyncio.to_thread(runtime.detail, extension_id))

    @app.get('/api/extensions/{extension_id}/plan')
    async def extension_install_plan(extension_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await asyncio.to_thread(runtime.install_plan, extension_id))

    @app.get('/api/environments')
    async def extension_environments() -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.environment_statuses())


    @app.get('/api/environments/storage')
    async def runtime_storage(refresh: bool = False) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await asyncio.to_thread(runtime.storage_status, refresh=refresh))

    @app.put('/api/environments/storage')
    async def set_runtime_storage(request: Request) -> F8JsonValue:
        runtime = get_runtime()
        payload = await decode_body(request, RuntimeStorageRequest)
        return _json_value(await asyncio.to_thread(runtime.extensions.set_runtime_storage, payload.path))

    @app.post('/api/environments/unused/clean', status_code=202)
    async def clean_unused_environments() -> F8JsonValue:
        return _json_value(get_runtime().submit_job(ManagementJobRequest(action='clean-unused-environments')))


    @app.get('/api/environments/{environment_id}/detail')
    async def environment_detail(environment_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await asyncio.to_thread(runtime.extensions.runtime_registry.detail, environment_id))

    @app.post('/api/environments/{environment_id}/prepare', status_code=202)
    async def prepare_environment(environment_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.submit_job(ManagementJobRequest(action='prepare-environment', environment_id=environment_id)))

    @app.post('/api/environments/{environment_id}/cancel', status_code=202)
    async def cancel_environment_preparation(environment_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await runtime.cancel_matching_job('prepare-environment', environment_id))


    @app.delete('/api/environments/{environment_id}', status_code=202)
    async def remove_environment(environment_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.submit_job(ManagementJobRequest(action='remove-environment', environment_id=environment_id)))


    @app.post('/api/extensions/{extension_id}/install', status_code=202)
    async def install_extension(extension_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.submit_job(ManagementJobRequest(action='install-extension', extension_id=extension_id)))

    @app.post('/api/extensions/{extension_id}/cancel', status_code=202)
    async def cancel_extension_install(extension_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await runtime.cancel_matching_job('install-extension', extension_id))

    @app.put('/api/extensions/{extension_id}/enabled', status_code=202)
    async def set_extension_enabled(extension_id: str, request: Request) -> F8JsonValue:
        runtime = get_runtime()
        payload = await decode_body(request, ExtensionToggleRequest)
        return _json_value(runtime.submit_job(ManagementJobRequest(
            action='enable-extension' if payload.enabled else 'disable-extension', extension_id=extension_id)))

    @app.delete('/api/extensions/{extension_id}', status_code=202)
    async def uninstall_extension(extension_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.submit_job(ManagementJobRequest(action='uninstall-extension', extension_id=extension_id)))

    @app.get('/api/inventory')
    async def inventory() -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await asyncio.to_thread(runtime.refresh))

    @app.get('/api/service-processes')
    async def service_processes() -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.services.statuses())

    @app.get('/api/service-processes/logs')
    async def process_logs(after: int = 0) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(runtime.services.logs(after))

    @app.post('/api/service-processes/{service_id}/start')
    async def start_service_process(service_id: str, request: Request) -> F8JsonValue:
        runtime = get_runtime()
        payload = await decode_body(request, ServiceStartRequest)
        return _json_value(await runtime.services.start(service_id, payload))

    @app.post('/api/service-processes/{service_id}/stop')
    async def stop_service_process(service_id: str) -> F8JsonValue:
        runtime = get_runtime()
        return _json_value(await runtime.services.stop(service_id))
