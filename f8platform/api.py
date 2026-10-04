"""Authenticated local component management, available without WebStudio."""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict

import msgspec

from .components import ComponentManager
from .instance import single_platform_instance
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServiceUnavailableError

logger = logging.getLogger(__name__)


class ImportComponent(BaseModel):
    model_config = ConfigDict(extra='forbid')
    location: str
    sha256: str


class ComponentConfiguration(BaseModel):
    model_config = ConfigDict(extra='forbid')
    endpoints: dict[str, str]


class ComponentVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sha256: str


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
    manager = ComponentManager(data_dir)
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
                for component_id in active_startup:
                    await manager.start(component_id)
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
    app.state.components = manager

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
        logger.exception('Component unavailable', exc_info=exc)
        return JSONResponse(status_code=503, content={'message': str(exc)})

    @app.get('/api/health')
    async def health() -> dict[str, str]:
        return {'status': 'ok', 'service': 'f8platform', 'protocolVersion': 'f8platform-api/1'}

    @app.get('/api/components')
    async def components() -> Response:
        return Response(msgspec.json.encode(manager.list()), media_type='application/json')

    @app.post('/api/components/import')
    async def import_component(request: ImportComponent) -> Response:
        record = await manager.import_archive(request.location, request.sha256)
        return Response(msgspec.json.encode(record), media_type='application/json', status_code=201)

    @app.post('/api/components/{component_id}/prepare', status_code=204)
    async def prepare(component_id: str, request: ComponentVersion) -> Response:
        await manager.prepare(component_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/components/{component_id}/select', status_code=204)
    async def select(component_id: str, request: ComponentVersion) -> Response:
        await manager.select(component_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/components/{component_id}/start', status_code=204)
    async def start(component_id: str) -> Response:
        await manager.start(component_id)
        return Response(status_code=204)

    @app.post('/api/components/{component_id}/stop', status_code=204)
    async def stop(component_id: str) -> Response:
        await manager.stop(component_id)
        return Response(status_code=204)

    @app.post('/api/components/{component_id}/uninstall', status_code=204)
    async def uninstall(component_id: str, request: ComponentVersion) -> Response:
        await manager.uninstall(component_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/components/{component_id}/update', status_code=204)
    async def update(component_id: str, request: ComponentVersion) -> Response:
        await manager.update(component_id, request.sha256)
        return Response(status_code=204)

    @app.post('/api/components/{component_id}/configure', status_code=204)
    async def configure(component_id: str, request: ComponentConfiguration) -> Response:
        await manager.configure(component_id, request.endpoints)
        return Response(status_code=204)

    return app
