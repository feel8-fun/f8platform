"""Platform-owned service processes, independent of any connected editor."""
from __future__ import annotations

import asyncio
from collections import deque
import logging
from threading import RLock

from f8pysdk.platform_spec import ProcessLog, ServiceProcessStatus, ServiceStartRequest
from f8pysdk.service_runtime_tools.deploy import ServiceProcessConfig, ServiceProcessManager
from f8pysdk.service_runtime_tools.inventory import ServiceCatalog

from .errors import ConflictError
from .extensions import ExtensionManager

logger = logging.getLogger(__name__)


class PlatformServices:
    def __init__(self, extensions: ExtensionManager, catalog: ServiceCatalog) -> None:
        self.extensions = extensions
        self.catalog = catalog
        self.manager = ServiceProcessManager(catalog_provider=lambda: self.catalog)
        self._classes: dict[str, str] = {}
        self._starting: dict[str, str] = {}
        self._logs: deque[ProcessLog] = deque(maxlen=2000)
        self._sequence = 0
        self._lock = RLock()
        self._closing = False

    def is_class_running(self, service_class: str) -> bool:
        return service_class in self._starting.values() or any(
            name == service_class and self.manager.is_running(identifier) for identifier, name in self._classes.items()
        )

    def statuses(self) -> tuple[ServiceProcessStatus, ...]:
        return tuple(ServiceProcessStatus(service_id=identifier, service_class=name,
                                         running=self.manager.is_running(identifier))
                     for identifier, name in self._classes.items())

    def logs(self, after: int) -> tuple[ProcessLog, ...]:
        with self._lock:
            return tuple(item for item in self._logs if item.sequence > after)

    def _output(self, identifier: str, line: str) -> None:
        line = line.rstrip()[:8192]
        if not line:
            return
        logger.info('Service %s: %s', identifier, line)
        with self._lock:
            self._sequence += 1
            self._logs.append(ProcessLog(sequence=self._sequence, service_id=identifier, line=line))

    async def start(self, identifier: str, request: ServiceStartRequest) -> ServiceProcessStatus:
        if self._closing:
            raise ConflictError('Platform is closing')
        if not self.extensions.service_enabled(request.service_class):
            raise ConflictError(f'Extension service is disabled or uninstalled: {request.service_class}')
        if identifier in self._starting or self.manager.is_running(identifier):
            raise ConflictError(f'Service is already running or starting: {identifier}')
        self._starting[identifier] = request.service_class
        config = ServiceProcessConfig(service_id=identifier, service_class=request.service_class,
            bus_backend=request.bus_backend, zenoh_config_path=request.zenoh_config_path,
            zenoh_connect=request.zenoh_connect, zenoh_listen=request.zenoh_listen,
            zenoh_shm_pool_bytes=request.zenoh_shm_pool_bytes)
        startup = asyncio.create_task(asyncio.to_thread(self.manager.start, config, on_output=self._output))
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            logger.info('Service startup cancelled: %s', identifier, exc_info=True)
            try:
                await startup
            finally:
                await asyncio.to_thread(self.manager.stop, identifier)
            raise
        finally:
            self._starting.pop(identifier, None)
        self._classes[identifier] = request.service_class
        return ServiceProcessStatus(service_id=identifier, service_class=request.service_class,
                                    running=self.manager.is_running(identifier))

    async def stop(self, identifier: str) -> ServiceProcessStatus:
        if identifier in self._starting:
            raise ConflictError(f'Service is starting: {identifier}')
        name = self._classes.get(identifier, '')
        await asyncio.to_thread(self.manager.stop, identifier)
        running = self.manager.is_running(identifier)
        if not running:
            self._classes.pop(identifier, None)
        return ServiceProcessStatus(service_id=identifier, service_class=name, running=running)

    async def close(self) -> None:
        self._closing = True
        for identifier in tuple(self._classes):
            await self.stop(identifier)
