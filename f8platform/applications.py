"""Immutable application installation and process lifecycle, independent of Studio."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import secrets
from typing import BinaryIO, Literal
import urllib.parse
import urllib.request

import httpx
import msgspec
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from f8pysdk.application_package import validate_application
from f8pysdk.application_spec import ApplicationManifest, ApplicationEndpoint
from f8pysdk.extension_spec import ExtensionManifest, ExtensionRuntime
from f8pysdk.release_spec import PublishedArtifact
from f8pysdk.specs import F8JsonValue
from f8pysdk.platform_spec import ApplicationRecord, ApplicationStatus

from .environments import EnvironmentManager
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServiceUnavailableError
from .extension_artifacts import MAX_ARCHIVE_BYTES, extract_archive
from .extension_operation import InstallOperation
from f8pysdk.extension_status import ExtensionInstallPlan

logger = logging.getLogger(__name__)


class ApplicationState(msgspec.Struct, frozen=True, kw_only=True, rename='camel'):
    records: tuple[ApplicationRecord, ...] = ()
    selected: dict[str, str] = msgspec.field(default_factory=dict)


@dataclass
class RunningApplication:
    process: asyncio.subprocess.Process
    log: BinaryIO
    instance: str


class ApplicationManager:
    @property
    def runtime_busy(self) -> bool:
        return self._actions.locked()

    @asynccontextmanager
    async def runtime_maintenance(self) -> AsyncGenerator[None]:
        async with self._actions:
            yield

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.root = self.data_dir / 'applications'
        self.root.mkdir(parents=True, exist_ok=True)
        config = self.data_dir / 'distribution/config'
        config.mkdir(parents=True, exist_ok=True)
        catalogs: tuple[tuple[str, dict[str, F8JsonValue]], ...] = (
            ('service-index.json', {'schemaVersion': 'f8serviceIndex/1', 'services': [], 'modelRoot': '${F8_MODEL_ROOT}'}),
            ('extensions.json', {'schemaVersion': 'f8extensionCatalog/1', 'extensions': [], 'preinstalled': []}),
        )
        for filename, content in catalogs:
            path = config / filename
            if not path.exists():
                try:
                    with path.open('xb') as handle:
                        handle.write(msgspec.json.encode(content))
                except FileExistsError:
                    logger.debug('Deployment catalog already initialized: %s', path, exc_info=True)
        self.state_path = self.root / 'state.json'
        self.state = (msgspec.json.decode(self.state_path.read_bytes(), type=ApplicationState)
                      if self.state_path.is_file() else ApplicationState())
        self.running: dict[str, RunningApplication] = {}
        self.require_external_dependents_stopped: Callable[[str], None] | None = None
        self.require_source_stopped: Callable[[str], None] | None = None
        self._actions = asyncio.Lock()
        settings = self.root / 'configuration.json'
        self.configuration = (msgspec.json.decode(settings.read_bytes(), type=dict[str, dict[str, str]])
                              if settings.is_file() else {})

    def _payload(self, sha256: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise InvalidRequestError('Application artifact requires SHA-256')
        return self.root / 'payloads' / sha256

    def _record(self, extension_id: str, sha256: str | None = None) -> ApplicationRecord:
        selected = sha256 or self.state.selected.get(extension_id)
        for record in self.state.records:
            if record.extension_id == extension_id and record.sha256 == selected:
                return record
        raise NotFoundError(f'Application {extension_id} has no matching installed release')

    def _save(self, state: ApplicationState) -> None:
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_bytes(msgspec.json.encode(state))
        temporary.replace(self.state_path)
        self.state = state

    def _runtime(self, record: ApplicationRecord) -> tuple[ApplicationManifest, EnvironmentManager, ExtensionManifest]:
        payload = self._payload(record.sha256)
        manifest = validate_application(payload)
        manager = EnvironmentManager(self.data_dir, payload)
        request = ExtensionManifest(extension_id=manifest.extension_id, name=manifest.title,
                                    version=manifest.version, description='Platform application runtime',
                                    runtime=ExtensionRuntime(kind='pixi', environment=manifest.launch.environment))
        return manifest, manager, request

    def environment_sources(self) -> tuple[tuple[EnvironmentManager, ExtensionManifest], ...]:
        return tuple((manager, request) for record in self.state.records
                     for _manifest, manager, request in (self._runtime(record),))

    def environment_plan(self, extension_id: str, sha256: str) -> ExtensionInstallPlan:
        _manifest, manager, request = self._runtime(self._record(extension_id, sha256))
        return manager.plan(request)

    def list(self) -> tuple[ApplicationStatus, ...]:
        statuses: list[ApplicationStatus] = []
        for record in self.state.records:
            manifest, manager, request = self._runtime(record)
            active = self.running.get(record.extension_id)
            selected = self.state.selected.get(record.extension_id) == record.sha256
            lifecycle: Literal['stopped', 'running', 'failed'] = 'stopped'
            if active is not None and selected:
                lifecycle = 'running' if active.process.returncode is None else 'failed'
            statuses.append(ApplicationStatus(manifest=manifest, sha256=record.sha256, selected=selected,
                                           prepared=manager.ready(manager.plan(request).environment_id),
                                           state=lifecycle, log_path=str(self._log(record.extension_id)),
                                           endpoints=self.endpoints(manifest)))
        return tuple(statuses)

    def _log(self, extension_id: str) -> Path:
        return self.root / 'logs' / f'{extension_id}.log'

    def import_local_archive(self, location: str, sha256: str) -> ApplicationRecord:
        payload = self._payload(sha256)
        if not payload.exists():
            cache = self.root / 'archives'
            cache.mkdir(exist_ok=True)
            archive = cache / f'{sha256}.zip'
            temporary = archive.with_suffix('.download')
            parsed = urllib.parse.urlsplit(location)
            try:
                if parsed.scheme == 'https':
                    if not parsed.hostname or parsed.username or parsed.password:
                        raise InvalidRequestError('Application downloads require HTTPS without credentials')
                    with urllib.request.urlopen(location, timeout=30) as source, temporary.open('wb') as output:
                        if urllib.parse.urlsplit(str(source.geturl())).scheme != 'https':
                            raise InvalidRequestError('Application download redirected away from HTTPS')
                        total = 0
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_ARCHIVE_BYTES:
                                raise InvalidRequestError('Application archive exceeds size limit')
                            output.write(chunk)
                else:
                    path = Path(location)
                    if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise InvalidRequestError('Application source must be HTTPS or an absolute local archive')
                    shutil.copyfile(path, temporary)
                with temporary.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != sha256:
                        raise InvalidRequestError('Application archive checksum mismatch')
                temporary.replace(archive)
                extract_archive(archive, payload, required=('config/extensions.json', 'config/artifact.json', 'config/runtime-environments.json'))
            finally:
                temporary.unlink(missing_ok=True)
        manifest = validate_application(payload)
        descriptor = msgspec.json.decode((payload / 'config/artifact.json').read_bytes(), type=PublishedArtifact)
        if (descriptor.kind != 'extension' or descriptor.artifact_id != manifest.extension_id
                or descriptor.version != manifest.version):
            raise InvalidRequestError('Application identity does not match artifact descriptor')
        record = ApplicationRecord(extension_id=manifest.extension_id, version=manifest.version, sha256=sha256)
        for previous in self.state.records:
            if previous.extension_id == record.extension_id and previous.version == record.version:
                if previous.sha256 != sha256:
                    raise ConflictError('Application release version already has different immutable content')
                return previous
        self._save(ApplicationState(records=(*self.state.records, record), selected=dict(self.state.selected)))
        return record

    async def import_archive(self, location: str, sha256: str) -> ApplicationRecord:
        async with self._actions:
            return await asyncio.to_thread(self.import_local_archive, location, sha256)

    async def prepare(self, extension_id: str, sha256: str) -> None:
        async with self._actions:
            record = self._record(extension_id, sha256)
            _manifest, manager, request = self._runtime(record)
            operation = InstallOperation(extension_id, self._log(extension_id))
            await asyncio.to_thread(manager.ensure, request, operation)

    def _manifests(self, selected: dict[str, str]) -> dict[str, ApplicationManifest]:
        return {name: validate_application(self._payload(self._record(name, digest).sha256))
                for name, digest in selected.items()}

    def validate_selection(self, selected: dict[str, str]) -> None:
        manifests = self._manifests(selected)
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ConflictError(f'Application dependency cycle includes {name}')
            if name in visited:
                return
            visiting.add(name)
            for required in manifests[name].requires:
                provider = manifests.get(required.extension_id)
                if provider is None:
                    raise ConflictError(f'{name} requires selected application {required.extension_id}')
                versions = {entry.protocol_id: entry.version for entry in provider.provides}
                version = versions.get(required.protocol_id)
                if version is None or Version(version) not in SpecifierSet(required.versions):
                    raise ConflictError(f'{name} requires {required.extension_id} protocol '
                                        f'{required.protocol_id}{required.versions}; provided {version}')
                visit(required.extension_id)
            visiting.remove(name)
            visited.add(name)

        for name in manifests:
            visit(name)

    async def select(self, extension_id: str, sha256: str) -> None:
        async with self._actions:
            self._record(extension_id, sha256)
            self._require_stopped(extension_id)
            selected = {**self.state.selected, extension_id: sha256}
            self.validate_selection(selected)
            self._save(ApplicationState(records=self.state.records, selected=selected))

    async def select_all(self, selected: dict[str, str]) -> None:
        async with self._actions:
            for name in set(self.state.selected) | selected.keys():
                self._require_stopped(name)
            self.validate_selection(selected)
            self._save(ApplicationState(records=self.state.records, selected=dict(selected)))

    async def deselect(self, extension_id: str) -> None:
        async with self._actions:
            self._require_stopped(extension_id)
            selected = dict(self.state.selected)
            selected.pop(extension_id, None)
            self.validate_selection(selected)
            self._save(ApplicationState(records=self.state.records, selected=selected))

    async def update(self, extension_id: str, sha256: str) -> None:
        async with self._actions:
            record = self._record(extension_id, sha256)
            previous = self._record(extension_id)
            self._require_stopped(extension_id, allow_self=True)
            selected = {**self.state.selected, extension_id: sha256}
            self.validate_selection(selected)
            _manifest, manager, request = self._runtime(record)
            await asyncio.to_thread(manager.ensure, request, InstallOperation(extension_id, self._log(extension_id)))
            active = self.running.get(extension_id)
            restart = active is not None and active.process.returncode is None
            await self._stop(extension_id)
            self._save(ApplicationState(records=self.state.records, selected=selected))
            started: list[str] = []
            try:
                if restart:
                    await self._start(extension_id, started)
            except (OSError, ValueError, ConflictError, ServiceUnavailableError, asyncio.CancelledError):
                logger.exception('Application update failed; restoring %s %s', extension_id, previous.version)
                for name in reversed(started):
                    await self._stop(name)
                restored = {**self.state.selected, extension_id: previous.sha256}
                self._save(ApplicationState(records=self.state.records, selected=restored))
                if restart:
                    await self._start(extension_id, [])
                raise

    def _require_stopped(self, extension_id: str, *, allow_self: bool = False) -> None:
        if self.require_external_dependents_stopped is not None:
            self.require_external_dependents_stopped(extension_id)
        affected = {extension_id}
        manifests = self._manifests(self.state.selected)
        changed = True
        while changed:
            changed = False
            for name, manifest in manifests.items():
                if name not in affected and any(item.extension_id in affected for item in manifest.requires):
                    affected.add(name)
                    changed = True
        for name in affected:
            if allow_self and name == extension_id:
                continue
            process = self.running.get(name)
            if process is not None and process.process.returncode is None:
                raise ConflictError(f'Stop running application {name} before changing {extension_id}')

    def endpoints(self, manifest: ApplicationManifest) -> tuple[ApplicationEndpoint, ...]:
        configured = self.configuration.get(manifest.extension_id, {})
        return tuple(ApplicationEndpoint(name=item.name, url=configured.get(item.name, item.url))
                     for item in manifest.endpoints)

    async def configure(self, extension_id: str, endpoints: dict[str, str]) -> None:
        async with self._actions:
            self._require_stopped(extension_id)
            manifest = validate_application(self._payload(self._record(extension_id).sha256))
            if endpoints.keys() - {item.name for item in manifest.endpoints}:
                raise InvalidRequestError('Configuration references an undeclared endpoint')
            for url in endpoints.values():
                parsed = urllib.parse.urlsplit(url)
                if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                        or not parsed.port or parsed.username or parsed.password or parsed.query
                        or parsed.fragment or parsed.path not in {'', '/'}):
                    raise InvalidRequestError('Application endpoint requires a loopback HTTP port')
            configuration = {**self.configuration, extension_id: dict(endpoints)}
            path = self.root / 'configuration.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(msgspec.json.encode(configuration))
            temporary.replace(path)
            self.configuration = configuration

    def _expand(self, manifest: ApplicationManifest, payload: Path, values: tuple[str, ...]) -> tuple[str, ...]:
        substitutions = {'${F8_PACKAGE_ROOT}': str(payload), '${F8_DATA_ROOT}': str(self.data_dir)}
        for name, provider in self._manifests(self.state.selected).items():
            for endpoint in self.endpoints(provider):
                substitutions[f'${{F8_ENDPOINT:{name}.{endpoint.name}}}'] = endpoint.url
                substitutions[f'${{F8_PORT:{name}.{endpoint.name}}}'] = str(urllib.parse.urlsplit(endpoint.url).port)
        result: list[str] = []
        for argument in values:
            value = argument
            for token, replacement in substitutions.items():
                value = value.replace(token, replacement)
            if '${' in value:
                raise InvalidRequestError(f'Unresolved application argument: {argument}')
            result.append(value)
        return tuple(result)

    async def start(self, extension_id: str) -> None:
        async with self._actions:
            self.validate_selection(self.state.selected)
            started: list[str] = []
            try:
                await self._start(extension_id, started)
            except (OSError, ValueError, ConflictError, ServiceUnavailableError, httpx.HTTPError, asyncio.CancelledError):
                logger.exception('Application startup failed: %s', extension_id)
                for name in reversed(started):
                    await self._stop(name)
                raise

    async def _start(self, extension_id: str, started: list[str]) -> None:
        if self.require_source_stopped is not None:
            self.require_source_stopped(extension_id)
        record = self._record(extension_id)
        manifest, manager, request = self._runtime(record)
        active = self.running.get(extension_id)
        if active is not None and active.process.returncode is None:
            await self._health(manifest, active.process, active.instance)
            return
        if active is not None:
            await self._stop(extension_id)
        for dependency in manifest.requires:
            await self._start(dependency.extension_id, started)
        plan = manager.plan(request)
        if not manager.ready(plan.environment_id):
            raise ConflictError(f'Prepare {extension_id} before starting it')
        executable, arguments = manager.python_launch(plan, manifest.launch.environment)
        log_path = self._log(extension_id)
        log_path.parent.mkdir(exist_ok=True)
        log = log_path.open('ab')
        instance = secrets.token_urlsafe(24)
        try:
            process = await asyncio.create_subprocess_exec(
                executable, *arguments, '-m', manifest.launch.module,
                *self._expand(manifest, self._payload(record.sha256), manifest.launch.args),
                cwd=self._payload(record.sha256), stdin=asyncio.subprocess.PIPE,
                stdout=log, stderr=log,
                env={**manager.install_environment(), 'F8_DATA_ROOT': str(self.data_dir),
                     'F8_PACKAGE_ROOT': str(self._payload(record.sha256)), 'F8_APPLICATION_INSTANCE': instance,
                     'F8_RUNTIME_STORAGE_ROOT': str(self.data_dir),
                     'F8_SERVICE_INDEX': str(self.data_dir / 'distribution/config/service-index.json'),
                     **dict(zip(manifest.launch.env, self._expand(manifest, self._payload(record.sha256),
                                                                 tuple(manifest.launch.env.values())), strict=True)),
                     'F8_PLATFORM_CONNECTION_FILE': str(self.data_dir / 'platform.json')},
                start_new_session=os.name != 'nt',
            )
        except OSError:
            log.close()
            raise
        self.running[extension_id] = RunningApplication(process, log, instance)
        started.append(extension_id)
        await self._health(manifest, process, instance)

    async def check_health(self, manifest: ApplicationManifest, process: asyncio.subprocess.Process, instance: str,
                           *, source_log: Path | None = None) -> None:
        await self._health(manifest, process, instance, source_log=source_log)

    async def stop_owned_process(self, active: RunningApplication) -> None:
        await stop_owned_process(active)

    async def _health(self, manifest: ApplicationManifest, process: asyncio.subprocess.Process, instance: str,
                      *, source_log: Path | None = None) -> None:
        probe = manifest.health
        endpoints = manifest.endpoints if source_log is not None else self.endpoints(manifest)
        endpoint = next(item.url for item in endpoints if item.name == probe.endpoint)
        log = source_log or self._log(manifest.extension_id)
        deadline = asyncio.get_running_loop().time() + probe.timeout_seconds
        last_error = 'No health response'
        bootstrapped = probe.bootstrap_path is None
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                if process.returncode is not None:
                    raise ServiceUnavailableError(f'{manifest.extension_id} exited ({process.returncode}); '
                                                  f'log: {log}')
                try:
                    if not bootstrapped:
                        bootstrap = await client.get(endpoint.rstrip('/') + (probe.bootstrap_path or '/'))
                        bootstrap.raise_for_status()
                        bootstrapped = True
                    response = await client.get(endpoint.rstrip('/') + probe.path)
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                else:
                    data = msgspec.json.decode(response.content, type=dict[str, object])
                    protocol = data.get('protocolVersion', data.get('protocol_version'))
                    if (data.get('service') != probe.service or protocol != probe.protocol_version
                            or data.get('applicationInstance', data.get('application_instance')) != instance
                            or data.get('version') != manifest.version):
                        raise ServiceUnavailableError(f'{manifest.extension_id} health identity/protocol mismatch')
                    if data.get('status') == 'ok':
                        return
                    last_error = f'Unexpected health status: {data.get("status")}'
                await asyncio.sleep(0.1)
        raise ServiceUnavailableError(f'{manifest.extension_id} startup timed out: {last_error}; '
                                      f'log: {log}')

    async def stop(self, extension_id: str) -> None:
        async with self._actions:
            if self.require_external_dependents_stopped is not None:
                self.require_external_dependents_stopped(extension_id)
            dependents = [name for name, manifest in self._manifests(self.state.selected).items()
                          if name != extension_id and any(item.extension_id == extension_id for item in manifest.requires)
                          and name in self.running and self.running[name].process.returncode is None]
            if dependents:
                raise ConflictError(f'Stop dependent applications first: {", ".join(dependents)}')
            await self._stop(extension_id)

    async def _stop(self, extension_id: str) -> None:
        active = self.running.pop(extension_id, None)
        if active is None:
            return
        await stop_owned_process(active)

    async def uninstall(self, extension_id: str, sha256: str) -> None:
        async with self._actions:
            self._record(extension_id, sha256)
            self._require_stopped(extension_id)
            selected = dict(self.state.selected)
            if selected.get(extension_id) == sha256:
                del selected[extension_id]
                self.validate_selection(selected)
            records = tuple(record for record in self.state.records if record.sha256 != sha256)
            self._save(ApplicationState(records=records, selected=selected))
            # Keep immutable payload/cache files and prefixes available for rollback/reimport.

    async def close(self) -> None:
        async with self._actions:
            for name in reversed(tuple(self.running)):
                await self._stop(name)


async def stop_owned_process(active: RunningApplication) -> None:
    process = active.process
    try:
        if process.returncode is None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    if os.name != 'nt':
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                    await process.wait()
    finally:
        active.log.close()
