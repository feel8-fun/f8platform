"""Explicit source applications; source execution never implies release installation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import shutil
import logging
from typing import BinaryIO
from urllib.parse import urlsplit
import httpx
import msgspec

from f8pysdk.platform_spec import DevelopmentApplication, DevelopmentCatalog, SourceApplicationStatus, SourceApplicationRegistration
from f8pysdk.application_spec import ApplicationEndpoint
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from .applications import ApplicationManager, RunningApplication
from .errors import ConflictError, NotFoundError, InvalidRequestError


class DevelopmentApplications:
    def __init__(self, data_dir: Path, catalog: DevelopmentCatalog, applications: ApplicationManager) -> None:
        self.root = data_dir
        self.definitions = {item.manifest.extension_id: item for item in catalog.applications}
        if len(self.definitions) != len(catalog.applications):
            raise ValueError('Duplicate development application identity')
        self.running: dict[str, RunningApplication] = {}
        self.applications = applications
        self._actions = asyncio.Lock()
        self.observed: dict[str, SourceApplicationRegistration] = {}
        self.observed_running: set[str] = set()
        self._probe_failures: set[str] = set()
        self.observed_path = data_dir / 'source-applications.json'
        if self.observed_path.is_file():
            previous = msgspec.json.decode(self.observed_path.read_bytes(), type=tuple[SourceApplicationRegistration, ...])
            for registration in previous:
                if registration.extension_id in self.definitions:
                    if registration.version == self.definitions[registration.extension_id].manifest.version:
                        self.register(registration)
                    else:
                        logging.getLogger(__name__).warning('Ignoring source instance from an older declaration: %s %s',
                                                             registration.extension_id, registration.version)

    def statuses(self) -> tuple[SourceApplicationStatus, ...]:
        return tuple(SourceApplicationStatus(extension_id=name, version=item.manifest.version,
            state=('running' if active.process.returncode is None else 'failed') if (active := self.running.get(name)) else 'stopped',
            endpoints=item.manifest.endpoints, log_path=str(self.root / 'source-logs' / f'{name}.log'))
            if name not in self.observed else SourceApplicationStatus(extension_id=name,
                version=self.observed[name].version, state='running' if name in self.observed_running else 'stopped',
                endpoints=(ApplicationEndpoint(name=item.manifest.health.endpoint, url=self.observed[name].url),),
                log_path='', managed=False)
            for name, item in self.definitions.items())

    def register(self, request: SourceApplicationRegistration) -> None:
        if request.extension_id not in self.definitions:
            raise NotFoundError('Source application is not declared in this workspace')
        if request.version != self.definitions[request.extension_id].manifest.version:
            raise InvalidRequestError('Source application version disagrees with its declaration')
        released = self.applications.running.get(request.extension_id)
        if released is not None and released.process.returncode is None:
            raise ConflictError('Stop the installed release before registering a source instance')
        if request.extension_id in self.running:
            active = self.running[request.extension_id]
            if active.instance != request.instance:
                raise ConflictError('A managed source instance is already registered')
            return
        address = urlsplit(request.url)
        if (address.scheme != 'http' or address.hostname not in {'localhost','127.0.0.1','::1'}
                or not address.port or address.username or address.password or address.path not in {'','/'}
                or address.query or address.fragment):
            raise InvalidRequestError('Source application requires a loopback HTTP endpoint')
        self.observed[request.extension_id] = request
        temporary = self.observed_path.with_suffix('.tmp')
        temporary.write_bytes(msgspec.json.encode(tuple(self.observed.values())))
        temporary.replace(self.observed_path)

    async def probe_observed(self) -> None:
        async with httpx.AsyncClient(timeout=2, trust_env=False, follow_redirects=True) as client:
            for name, request in tuple(self.observed.items()):
                manifest = self.definitions[name].manifest
                try:
                    if manifest.health.bootstrap_path is not None:
                        await client.get(request.url + manifest.health.bootstrap_path)
                    response = await client.get(request.url + manifest.health.path)
                    response.raise_for_status()
                    payload = response.json()
                    valid = (payload.get('applicationInstance', payload.get('application_instance')) == request.instance
                        and payload.get('service') == manifest.health.service
                        and payload.get('protocolVersion', payload.get('protocol_version')) == manifest.health.protocol_version
                        and payload.get('version') == request.version and payload.get('status') == 'ok')
                    if valid:
                        self.observed_running.add(name)
                        self._probe_failures.discard(name)
                    else:
                        self.observed_running.discard(name)
                        if name not in self._probe_failures:
                            logging.getLogger(__name__).warning('Source application identity mismatch: %s', name)
                            self._probe_failures.add(name)
                except (httpx.HTTPError, ValueError):
                    self.observed_running.discard(name)
                    if name not in self._probe_failures:
                        logging.getLogger(__name__).warning('Source application probe failed: %s', name, exc_info=True)
                        self._probe_failures.add(name)

    async def start(self, name: str) -> None:
        async with self._actions:
            started: list[str] = []
            try:
                await self._start(name, started, set())
            except (OSError, ValueError, RuntimeError, asyncio.CancelledError):
                import logging
                logging.getLogger(__name__).exception('Source application startup failed: %s', name)
                for identifier in reversed(started):
                    await self._stop(identifier)
                raise

    def require_dependents_stopped(self, name: str) -> None:
        active_names = self.observed_running | {
            identifier for identifier, active in self.running.items() if active.process.returncode is None
        }
        for identifier in active_names:
            if any(dependency.extension_id == name for dependency in self.definitions[identifier].manifest.requires):
                raise ConflictError(f'Stop dependent source application {identifier} before changing {name}')

    def require_source_stopped(self, name: str) -> None:
        active = self.running.get(name)
        if (active is not None and active.process.returncode is None) or name in self.observed_running:
            raise ConflictError(f'Stop source application {name} before starting an installed release')

    def expand(self, values: tuple[str, ...]) -> tuple[str, ...]:
        providers = {name: item.manifest.endpoints for name, item in self.definitions.items()}
        providers.update({item.manifest.extension_id: item.endpoints
                          for item in self.applications.list() if item.selected})
        result: list[str] = []
        for argument in values:
            for name, endpoints in providers.items():
                for endpoint in endpoints:
                    argument = argument.replace(f'${{F8_ENDPOINT:{name}.{endpoint.name}}}', endpoint.url)
                    argument = argument.replace(f'${{F8_PORT:{name}.{endpoint.name}}}', str(urlsplit(endpoint.url).port))
            if '${' in argument:
                raise InvalidRequestError(f'Unresolved source application argument: {argument}')
            result.append(argument)
        return tuple(result)

    async def _start(self, name: str, started: list[str], visiting: set[str]) -> None:
        item = self.definitions.get(name)
        if item is None:
            raise NotFoundError(f'Unknown source application: {name}')
        await self.probe_observed()
        if name in self.observed_running:
            raise ConflictError('This source application is running outside the Launcher; stop it at its original entrypoint')
        self.observed.pop(name, None)
        if name in visiting:
            raise ConflictError('Source application dependency cycle')
        if name in self.applications.state.selected:
            raise ConflictError(f'A release of {name} is selected; stop and deselect it before source debugging')
        active = self.running.get(name)
        if active is not None and active.process.returncode is None:
            await self.applications.check_health(item.manifest, active.process, active.instance,
                                                source_log=self.root / 'source-logs' / f'{name}.log')
            return
        visiting.add(name)
        releases = {item.manifest.extension_id: item for item in self.applications.list() if item.selected}
        for dependency in item.manifest.requires:
            provider = self.definitions.get(dependency.extension_id)
            release = releases.get(dependency.extension_id)
            manifest = release.manifest if release is not None else provider.manifest if provider is not None else None
            if manifest is None or not any(protocol.protocol_id == dependency.protocol_id
                and Version(protocol.version) in SpecifierSet(dependency.versions) for protocol in manifest.provides):
                raise ConflictError(f'Incompatible source application dependency: {dependency.extension_id}')
            if release is not None:
                await self.applications.start(dependency.extension_id)
            elif dependency.extension_id not in self.observed_running:
                await self._start(dependency.extension_id, started, visiting)
        visiting.remove(name)
        if active is not None:
            await self._stop(name)
        instance = secrets.token_urlsafe(24)
        path = self.root / 'source-logs' / f'{name}.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        arguments = self.expand(item.arguments)
        environment = dict(zip(item.environment, self.expand(tuple(item.environment.values())), strict=True))
        log = path.open('ab')
        started.append(name)
        try:
            executable, runtime_arguments = await self._prepare_runtime(item, instance, log)
            process = await asyncio.create_subprocess_exec(executable, *runtime_arguments, *arguments,
                cwd=item.workdir, env={**os.environ, **environment, 'F8_APPLICATION_INSTANCE': instance},
                stdin=asyncio.subprocess.PIPE, stdout=log, stderr=log, start_new_session=os.name != 'nt')
        except (OSError, RuntimeError, asyncio.CancelledError):
            if name not in self.running:
                log.close()
            raise
        self.running[name] = RunningApplication(process, log, instance)
        await self.applications.check_health(item.manifest, process, instance, source_log=path)

    async def _prepare_runtime(self, item: DevelopmentApplication, instance: str, log: BinaryIO) -> tuple[str, list[str]]:
        manifest = Path(item.runtime_manifest)
        if not manifest.is_file():
            raise FileNotFoundError(f'Source application runtime manifest is missing: {manifest}')
        executable = shutil.which('pixi')
        if executable is None:
            raise FileNotFoundError('Pixi is required to prepare the source application environment')
        environment = item.manifest.launch.environment
        process = await asyncio.create_subprocess_exec(executable, 'install', '--locked',
            '--manifest-path', str(manifest), '-e', environment, cwd=manifest.parent,
            stdin=asyncio.subprocess.PIPE, stdout=log, stderr=log, start_new_session=os.name != 'nt')
        self.running[item.manifest.extension_id] = RunningApplication(process, log, instance)
        returncode = await process.wait()
        if returncode != 0:
            raise RuntimeError(f'Cannot prepare source environment {environment}: Pixi exited ({returncode}); '
                               f'log: {self.root / "source-logs" / (item.manifest.extension_id + ".log")}')
        if process.stdin is not None:
            process.stdin.close()
        return executable, ['run', '--locked', '--no-install', '--manifest-path', str(manifest), '-e', environment, 'python']

    async def stop(self, name: str) -> None:
        async with self._actions:
            if name in self.observed:
                raise ConflictError('This source application is externally managed; stop it at its original entrypoint')
            self.require_dependents_stopped(name)
            await self._stop(name)

    async def _stop(self, name: str) -> None:
        active = self.running.pop(name, None)
        if active is None:
            return
        await self.applications.stop_owned_process(active)

    async def close(self) -> None:
        for name in reversed(tuple(self.running)):
            await self._stop(name)
