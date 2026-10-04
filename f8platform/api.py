"""Authenticated local application management, available without WebStudio."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from f8pysdk.platform_api import ApplicationConfiguration, ApplicationVersion, ImportApplication

import msgspec

from .applications import ApplicationManager
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
               open_browser: bool = False) -> FastAPI:
    manager = ApplicationManager(data_dir)
    token = access_token(data_dir)

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if authorization is None or not secrets.compare_digest(authorization, f'Bearer {token}'):
            raise HTTPException(status_code=401, detail='Platform token required')

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        with single_platform_instance(data_dir):
            try:
                active_startup = startup
                if distribution is not None:
                    from .distribution import install_distribution
                    bundled_startup = await install_distribution(manager, distribution)
                    active_startup = tuple(dict.fromkeys((*bundled_startup, *startup)))
                for extension_id in active_startup:
                    await manager.start(extension_id)
                if open_browser:
                    import webbrowser
                    for status in manager.list():
                        if status.selected and status.manifest.web_assets is not None and status.state == 'running':
                            webbrowser.open(next(item.url for item in status.endpoints if item.name == status.manifest.health.endpoint))
                            break
                yield
            finally:
                await manager.close()

    app = FastAPI(title='Feel8 Platform', version='0.1.0', lifespan=lifespan,
                  dependencies=[Depends(authorize)], docs_url=None, redoc_url=None, openapi_url=None)
    app.state.applications = manager

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

    @app.get('/api/applications')
    async def applications() -> Response:
        return Response(msgspec.json.encode(manager.list()), media_type='application/json')

    @app.post('/api/applications/import')
    async def import_application(request: ImportApplication) -> Response:
        record = await manager.import_archive(request.location, request.sha256)
        return Response(msgspec.json.encode(record), media_type='application/json', status_code=201)

    @app.post('/api/applications/{extension_id}/prepare', status_code=204)
    async def prepare(extension_id: str, request: ApplicationVersion) -> Response:
        await manager.prepare(extension_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/applications/{extension_id}/select', status_code=204)
    async def select(extension_id: str, request: ApplicationVersion) -> Response:
        await manager.select(extension_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/applications/{extension_id}/start', status_code=204)
    async def start(extension_id: str) -> Response:
        await manager.start(extension_id)
        return Response(status_code=204)

    @app.post('/api/applications/{extension_id}/stop', status_code=204)
    async def stop(extension_id: str) -> Response:
        await manager.stop(extension_id)
        return Response(status_code=204)

    @app.post('/api/applications/{extension_id}/uninstall', status_code=204)
    async def uninstall(extension_id: str, request: ApplicationVersion) -> Response:
        await manager.uninstall(extension_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/applications/{extension_id}/update', status_code=204)
    async def update(extension_id: str, request: ApplicationVersion) -> Response:
        await manager.update(extension_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/applications/{extension_id}/configure', status_code=204)
    async def configure(extension_id: str, request: ApplicationConfiguration) -> Response:
        await manager.configure(extension_id, request.endpoints)
        return Response(status_code=204)

    return app
