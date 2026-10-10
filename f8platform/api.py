"""Authenticated local application management, available without WebStudio."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from collections.abc import Callable
from contextlib import asynccontextmanager
import logging
import asyncio
import httpx
from .applications import ApplicationManager
from pathlib import Path
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from f8pysdk.platform_client import PlatformConnection
from .runtime import PlatformRuntime
from .management_api import install_management_routes, decode_body
from .portal import install_portal
from f8pysdk.platform_spec import SourceApplicationRegistration
from f8pysdk.platform_api import ApplicationConfiguration, ApplicationVersion, ImportApplication, PlatformStartup
from f8pysdk.management_job import ManagementJobRequest

import msgspec

from .instance import single_platform_instance
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServiceUnavailableError

logger = logging.getLogger(__name__)


def access_token(data_dir: Path) -> str:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / 'platform-token'
    if not path.exists():
        try:
            with path.open('x', encoding='utf-8') as output:
                path.chmod(0o600)
                output.write(secrets.token_urlsafe(32))
        except FileExistsError:
            logger.debug('Another platform client created access token', exc_info=True)
    token = path.read_text().strip()
    if not token:
        raise ValueError(f'Empty platform token: {path}')
    return token


def create_app(data_dir: Path, *, startup: tuple[str, ...] = (), distribution: Path | None = None,
               open_browser: bool = False, source_index: Path | None = None,
               development: Path | None = None, url: str = "http://127.0.0.1:8209",
               shutdown: Callable[[], None] | None = None) -> FastAPI:
    runtime: PlatformRuntime | None = None

    def get_runtime() -> PlatformRuntime:
        if runtime is None:
            raise ServiceUnavailableError('Platform has not started')
        return runtime
    token = access_token(data_dir)

    async def authorize(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        bearer = authorization is not None and secrets.compare_digest(authorization, f'Bearer {token}')
        cookie = secrets.compare_digest(request.cookies.get('f8platform', ''), token)
        if cookie and not bearer:
            origin = request.headers.get('origin')
            if request.headers.get('sec-fetch-site') == 'cross-site' or (origin is not None and origin != url):
                raise HTTPException(status_code=403, detail='Platform origin rejected')
            if request.method not in {'GET', 'HEAD'} and origin != url:
                raise HTTPException(status_code=403, detail='Platform origin required')
        if not bearer and not cookie:
            raise HTTPException(status_code=401, detail='Platform token required')

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        nonlocal runtime
        connection_path = data_dir / 'platform.json'
        with single_platform_instance(data_dir):
            manager = ApplicationManager(data_dir)
            boot: asyncio.Task[None] | None = None
            try:
                startup_path = data_dir / 'startup.json'
                saved = PlatformStartup.model_validate_json(startup_path.read_bytes()).applications if startup_path.is_file() else ()
                active_startup = tuple(dict.fromkeys((*saved, *startup)))
                if distribution is not None:
                    from .distribution import install_distribution
                    bundled_startup = await install_distribution(manager, distribution)
                    active_startup = tuple(dict.fromkeys((*bundled_startup, *active_startup)))
                runtime = PlatformRuntime(data_dir, source_index=source_index, development=development, applications=manager)
                app.state.platform = runtime
                connection_path.write_bytes(msgspec.json.encode(PlatformConnection(url=url, token_file=str((data_dir / 'platform-token').resolve()))))

                async def activate() -> None:
                    try:
                        # Web applications connect back to this daemon during construction.
                        # Wait until HTTP is listening, after yielding the ASGI lifespan.
                        async with httpx.AsyncClient(timeout=1, trust_env=False,
                            headers={'Authorization': f'Bearer {token}'}) as client:
                            for _attempt in range(300):
                                try:
                                    response = await client.get(url + '/api/health')
                                    if response.is_success:
                                        break
                                except httpx.ConnectError:
                                    logger.debug('Waiting for platform HTTP listener', exc_info=True)
                                await asyncio.sleep(0.1)
                            else:
                                raise ServiceUnavailableError('Platform HTTP listener did not become ready')
                        active = get_runtime()
                        await active.development.probe_observed()
                        for extension_id in active_startup:
                            if extension_id in active.development.definitions and extension_id not in manager.state.selected:
                                await active.development.start(extension_id)
                            else:
                                await manager.start(extension_id)
                        if open_browser:
                            import webbrowser
                            if not webbrowser.open(f'{url}/bootstrap/{token}'):
                                logger.warning('No browser available for the platform portal')
                        app.state.startup_error = ''
                    except (OSError, ValueError, RuntimeError, httpx.HTTPError):
                        logger.exception('Platform automatic application startup failed')
                        app.state.startup_error = 'Automatic startup failed. See the platform console log.'

                if active_startup or open_browser:
                    boot = asyncio.create_task(activate(), name='platform-startup')
                yield
            finally:
                if boot is not None:
                    boot.cancel()
                    await asyncio.gather(boot, return_exceptions=True)
                if runtime is not None:
                    await runtime.close()
                else:
                    await manager.close()
                connection_path.unlink(missing_ok=True)

    app = FastAPI(title='Feel8 Platform', version='0.1.0', lifespan=lifespan,
                  dependencies=[Depends(authorize)], docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', '::1', 'testserver'])
    install_management_routes(app, get_runtime)

    @app.exception_handler(ConflictError)
    async def conflict(_request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={'message': str(exc)})

    @app.exception_handler(NotFoundError)
    async def missing(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={'message': str(exc)})

    @app.exception_handler(InvalidRequestError)
    async def invalid(_request: Request, exc: InvalidRequestError) -> JSONResponse:
        return JSONResponse(status_code=400, content={'message': str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, exc: ValueError) -> JSONResponse:
        logger.exception('Invalid platform operation', exc_info=exc)
        return JSONResponse(status_code=400, content={'message': str(exc)})

    @app.exception_handler(ServiceUnavailableError)
    async def unavailable(_request: Request, exc: ServiceUnavailableError) -> JSONResponse:
        logger.exception('Application unavailable', exc_info=exc)
        return JSONResponse(status_code=503, content={'message': str(exc)})

    @app.get('/api/health')
    async def health() -> dict[str, str]:
        return {'status': 'ok', 'service': 'f8platform', 'protocolVersion': 'f8platform-api/1'}

    @app.post('/api/shutdown', status_code=204)
    async def shutdown_platform() -> Response:
        if shutdown is None:
            raise ConflictError('This embedded platform has no process shutdown callback')
        shutdown()
        return Response(status_code=204)

    @app.get('/api/applications')
    async def applications() -> Response:
        runtime = get_runtime()
        manager = runtime.applications
        return Response(msgspec.json.encode(manager.list()), media_type='application/json')

    @app.get('/api/application-dependencies/{extension_id}')
    async def dependency_endpoints(extension_id: str) -> Response:
        endpoints = await get_runtime().dependency_endpoints(extension_id, start=False)
        return Response(msgspec.json.encode(endpoints), media_type='application/json')

    @app.post('/api/application-dependencies/{extension_id}')
    async def start_dependencies(extension_id: str) -> Response:
        endpoints = await get_runtime().dependency_endpoints(extension_id, start=True)
        return Response(msgspec.json.encode(endpoints), media_type='application/json')

    @app.get('/api/startup')
    async def startup_configuration() -> PlatformStartup:
        path = data_dir / 'startup.json'
        return PlatformStartup.model_validate_json(path.read_bytes()) if path.is_file() else PlatformStartup(applications=())

    @app.put('/api/startup')
    async def set_startup_configuration(request: PlatformStartup) -> PlatformStartup:
        active = get_runtime()
        known = {item.extension_id for item in active.statuses() if item.application}
        if set(request.applications) - known or len(request.applications) != len(set(request.applications)):
            raise InvalidRequestError('Startup requires distinct known application identities')
        target = data_dir / 'startup.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_text(request.model_dump_json())
        temporary.replace(target)
        return request

    @app.get('/api/application-logs/{extension_id}')
    async def application_logs(extension_id: str) -> dict[str, str]:
        active = get_runtime()
        status = active.status(extension_id)
        source = status.running_source or (extension_id in active.development.definitions and status.release_sha256 is None)
        path = data_dir / ('source-logs' if source else 'applications/logs') / f'{extension_id}.log'
        if not path.is_file():
            return {'log': ''}
        with path.open('rb') as log:
            log.seek(max(0, path.stat().st_size - 1024 * 1024))
            return {'log': log.read().decode('utf-8', errors='replace')}

    @app.post('/api/applications/import', status_code=202)
    async def import_application(request: Request) -> Response:
        payload = await decode_body(request, ImportApplication)
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='import-application', location=payload.location, sha256=payload.sha256))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/prepare', status_code=202)
    async def prepare(extension_id: str, request: Request) -> Response:
        payload = await decode_body(request, ApplicationVersion)
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='prepare-application', extension_id=extension_id, sha256=payload.sha256))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/select', status_code=202)
    async def select(extension_id: str, request: Request) -> Response:
        payload = await decode_body(request, ApplicationVersion)
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='select-application', extension_id=extension_id, sha256=payload.sha256))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/deselect', status_code=202)
    async def deselect(extension_id: str) -> Response:
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='deselect-application', extension_id=extension_id))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.get('/api/source-applications')
    async def source_applications() -> Response:
        runtime = get_runtime()
        await runtime.development.probe_observed()
        return Response(msgspec.json.encode(runtime.development.statuses()), media_type='application/json')

    @app.post('/api/source-applications/register', status_code=204)
    async def register_source(request: Request) -> Response:
        try:
            registration = msgspec.json.decode(await request.body(), type=SourceApplicationRegistration)
        except msgspec.DecodeError as exc:
            raise InvalidRequestError(str(exc)) from exc
        get_runtime().development.register(registration)
        return Response(status_code=204)

    @app.post('/api/source-applications/{extension_id}/start', status_code=202)
    async def start_source(extension_id: str) -> Response:
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='start-source', extension_id=extension_id))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/source-applications/{extension_id}/stop', status_code=202)
    async def stop_source(extension_id: str) -> Response:
        runtime = get_runtime()
        operation = runtime.stop_application(extension_id, source=True)
        return Response(msgspec.json.encode(operation), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/start', status_code=202)
    async def start(extension_id: str) -> Response:
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='start-application', extension_id=extension_id))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/source-applications/{extension_id}/restart', status_code=202)
    async def restart_source(extension_id: str) -> Response:
        job = get_runtime().submit_job(ManagementJobRequest(action='restart-source', extension_id=extension_id))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/restart', status_code=202)
    async def restart(extension_id: str) -> Response:
        job = get_runtime().submit_job(ManagementJobRequest(action='restart-application', extension_id=extension_id))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/stop', status_code=202)
    async def stop(extension_id: str) -> Response:
        runtime = get_runtime()
        operation = runtime.stop_application(extension_id, source=False)
        return Response(msgspec.json.encode(operation), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/uninstall', status_code=202)
    async def uninstall(extension_id: str, request: Request) -> Response:
        payload = await decode_body(request, ApplicationVersion)
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='uninstall-application', extension_id=extension_id, sha256=payload.sha256))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/update', status_code=202)
    async def update(extension_id: str, request: Request) -> Response:
        payload = await decode_body(request, ApplicationVersion)
        runtime = get_runtime()
        job = runtime.submit_job(ManagementJobRequest(action='update-application', extension_id=extension_id, sha256=payload.sha256))
        return Response(msgspec.json.encode(job), media_type='application/json', status_code=202)

    @app.post('/api/applications/{extension_id}/configure', status_code=204)
    async def configure(extension_id: str, request: ApplicationConfiguration) -> Response:
        runtime = get_runtime()
        manager = runtime.applications
        await manager.configure(extension_id, request.endpoints)
        return Response(status_code=204)

    install_portal(app, token=token)
    return app
