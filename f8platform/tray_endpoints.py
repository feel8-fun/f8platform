"""Browser shortcuts derived from application inventory, never application IDs."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
from threading import Event
from time import monotonic
from urllib.parse import urlsplit

import httpx
import msgspec

from f8pysdk.platform_client import PlatformClient
from f8pysdk.platform_errors import ServiceUnavailableError
from f8pysdk.platform_spec import ApplicationStatus, SourceApplicationStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrayEndpoint:
    label: str
    url: str
    running: bool


def application_shortcuts(
    applications: tuple[ApplicationStatus, ...], sources: tuple[SourceApplicationStatus, ...],
) -> tuple[TrayEndpoint, ...]:
    entries: list[TrayEndpoint] = []
    for application in applications:
        label = f'{application.manifest.title} · v{application.manifest.version}'
        for endpoint in application.endpoints:
            if urlsplit(endpoint.url).scheme in {'http', 'https'}:
                title = f'{label} / {endpoint.name}' if len(application.endpoints) > 1 else label
                entries.append(TrayEndpoint(f'Open {title}', endpoint.url, application.state == 'running'))
    titles = {application.manifest.extension_id: application.manifest.title for application in applications}
    for source in sources:
        label = f'{titles.get(source.extension_id, source.extension_id)} (source)'
        for endpoint in source.endpoints:
            if urlsplit(endpoint.url).scheme in {'http', 'https'}:
                title = f'{label} / {endpoint.name}' if len(source.endpoints) > 1 else label
                entries.append(TrayEndpoint(f'Open {title}', endpoint.url, source.state == 'running'))
    return tuple(sorted(entries, key=lambda entry: (entry.label, entry.url)))


def watch_endpoints(
    client: PlatformClient, stop: Event, publish: Callable[[tuple[TrayEndpoint, ...]], None],
    *, interval: float = 2.0, startup_timeout: float = 30.0,
) -> None:
    """Poll off the UI thread; disable stale shortcuts when Platform is unreachable."""
    entries: tuple[TrayEndpoint, ...] = ()
    last_error: tuple[type[Exception], str] | None = None
    startup_deadline = monotonic() + startup_timeout
    connected = False
    waiting_reported = False
    try:
        while not stop.is_set():
            try:
                applications = client.read('GET', '/api/applications', tuple[ApplicationStatus, ...])
                sources = client.read('GET', '/api/source-applications', tuple[SourceApplicationStatus, ...])
                updated = application_shortcuts(applications, sources)
                if not connected and waiting_reported:
                    logger.info('Platform is ready; tray application shortcuts are active')
                connected = True
                last_error = None
            except (ServiceUnavailableError, OSError, msgspec.DecodeError) as exc:
                # The tray starts alongside the daemon, before its HTTP listener.
                # Retry only initial connection failures, not auth/API errors.
                if (not connected and monotonic() < startup_deadline
                        and isinstance(exc, ServiceUnavailableError)
                        and isinstance(exc.__cause__, httpx.ConnectError)):
                    if not waiting_reported:
                        logger.info('Waiting for Platform to start (up to %.0f seconds)', startup_timeout)
                        logger.debug('Platform HTTP listener is not ready yet', exc_info=True)
                        waiting_reported = True
                    stop.wait(min(interval, 0.1))
                    continue
                error = (type(exc), str(exc))
                if error != last_error:
                    if (not connected and isinstance(exc, ServiceUnavailableError)
                            and isinstance(exc.__cause__, httpx.ConnectError)):
                        logger.warning('Cannot refresh tray application shortcuts: Platform startup timed out after %.0f seconds',
                                       startup_timeout, exc_info=True)
                    else:
                        logger.warning('Cannot refresh tray application shortcuts', exc_info=True)
                    last_error = error
                updated = tuple(replace(entry, running=False) for entry in entries)
            if stop.is_set():
                break
            if updated != entries:
                entries = updated
                publish(entries)
            stop.wait(interval)
    finally:
        client.close()
