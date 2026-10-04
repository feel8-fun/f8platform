"""Immutable component installation and process lifecycle, independent of Studio."""
from __future__ import annotations

import asyncio
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

from f8pysdk.component_package import validate_component
from f8pysdk.component_spec import ComponentManifest, ComponentEndpoint
from f8pysdk.extension_spec import ExtensionManifest, ExtensionRuntime
from f8pysdk.release_spec import PublishedArtifact

from .environments import EnvironmentManager
from .errors import ConflictError, InvalidRequestError, NotFoundError, ServiceUnavailableError
from .extension_artifacts import MAX_ARCHIVE_BYTES, extract_archive
from .extension_operation import InstallOperation

logger = logging.getLogger(__name__)


class ComponentRecord(msgspec.Struct, frozen=True, kw_only=True, rename='camel'):
    component_id: str
    version: str
    sha256: str


class ComponentState(msgspec.Struct, frozen=True, kw_only=True, rename='camel'):
    records: tuple[ComponentRecord, ...] = ()
    selected: dict[str, str] = msgspec.field(default_factory=dict)


class ComponentStatus(msgspec.Struct, frozen=True, kw_only=True, rename='camel'):
    manifest: ComponentManifest
    sha256: str
    selected: bool
    prepared: bool
    state: Literal['stopped', 'running', 'failed']
    log_path: str
    endpoints: tuple[ComponentEndpoint, ...]


@dataclass
class RunningComponent:
    process: asyncio.subprocess.Process
    log: BinaryIO
    instance: str


class ComponentManager:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.root = self.data_dir / 'components'
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / 'state.json'
        self.state = (msgspec.json.decode(self.state_path.read_bytes(), type=ComponentState)
                      if self.state_path.is_file() else ComponentState())
        self.running: dict[str, RunningComponent] = {}
        self._actions = asyncio.Lock()
        settings = self.root / 'configuration.json'
        self.configuration = (msgspec.json.decode(settings.read_bytes(), type=dict[str, dict[str, str]])
                              if settings.is_file() else {})

    def _payload(self, sha256: str) -> Path:
        if not re.fullmatch(r'[0-9a-f]{64}', sha256):
            raise InvalidRequestError('Component artifact requires SHA-256')
        return self.root / 'payloads' / sha256

    def _record(self, component_id: str, sha256: str | None = None) -> ComponentRecord:
        selected = sha256 or self.state.selected.get(component_id)
        for record in self.state.records:
            if record.component_id == component_id and record.sha256 == selected:
                return record
        raise NotFoundError(f'Component {component_id} has no matching installed release')

    def _save(self, state: ComponentState) -> None:
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_bytes(msgspec.json.encode(state))
        temporary.replace(self.state_path)
        self.state = state

    def _runtime(self, record: ComponentRecord) -> tuple[ComponentManifest, EnvironmentManager, ExtensionManifest]:
        payload = self._payload(record.sha256)
        manifest = validate_component(payload)
        manager = EnvironmentManager(self.data_dir, payload)
        request = ExtensionManifest(extension_id=manifest.component_id, name=manifest.title,
                                    version=manifest.version, description='Platform component runtime',
                                    runtime=ExtensionRuntime(kind='pixi', environment=manifest.launch.environment))
        return manifest, manager, request

    def list(self) -> tuple[ComponentStatus, ...]:
        statuses: list[ComponentStatus] = []
        for record in self.state.records:
            manifest, manager, request = self._runtime(record)
            active = self.running.get(record.component_id)
            selected = self.state.selected.get(record.component_id) == record.sha256
            lifecycle: Literal['stopped', 'running', 'failed'] = 'stopped'
            if active is not None and selected:
                lifecycle = 'running' if active.process.returncode is None else 'failed'
            statuses.append(ComponentStatus(manifest=manifest, sha256=record.sha256, selected=selected,
                                           prepared=manager.ready(manager.plan(request).environment_id),
                                           state=lifecycle, log_path=str(self._log(record.component_id)),
                                           endpoints=self.endpoints(manifest)))
        return tuple(statuses)

    def _log(self, component_id: str) -> Path:
        return self.root / 'logs' / f'{component_id}.log'

    def import_local_archive(self, location: str, sha256: str) -> ComponentRecord:
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
                        raise InvalidRequestError('Component downloads require HTTPS without credentials')
                    with urllib.request.urlopen(location, timeout=30) as source, temporary.open('wb') as output:
                        if urllib.parse.urlsplit(str(source.geturl())).scheme != 'https':
                            raise InvalidRequestError('Component download redirected away from HTTPS')
                        total = 0
                        while chunk := source.read(1024 * 1024):
                            total += len(chunk)
                            if total > MAX_ARCHIVE_BYTES:
                                raise InvalidRequestError('Component archive exceeds size limit')
                            output.write(chunk)
                else:
                    path = Path(location)
                    if not path.is_absolute() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE_BYTES:
                        raise InvalidRequestError('Component source must be HTTPS or an absolute local archive')
                    shutil.copyfile(path, temporary)
                with temporary.open('rb') as source:
                    if hashlib.file_digest(source, 'sha256').hexdigest() != sha256:
                        raise InvalidRequestError('Component archive checksum mismatch')
                temporary.replace(archive)
                extract_archive(archive, payload, required=('component.json', 'artifact.json', 'config/runtime-environments.json'))
            finally:
                temporary.unlink(missing_ok=True)
        manifest = validate_component(payload)
        descriptor = msgspec.json.decode((payload / 'artifact.json').read_bytes(), type=PublishedArtifact)
        if (descriptor.kind != 'component' or descriptor.artifact_id != manifest.component_id
                or descriptor.version != manifest.version):
            raise InvalidRequestError('Component identity does not match artifact descriptor')
        record = ComponentRecord(component_id=manifest.component_id, version=manifest.version, sha256=sha256)
        for previous in self.state.records:
            if previous.component_id == record.component_id and previous.version == record.version:
                if previous.sha256 != sha256:
                    raise ConflictError('Component release version already has different immutable content')
                return previous
        self._save(ComponentState(records=(*self.state.records, record), selected=dict(self.state.selected)))
        return record

    async def import_archive(self, location: str, sha256: str) -> ComponentRecord:
        async with self._actions:
            return await asyncio.to_thread(self.import_local_archive, location, sha256)

    async def prepare(self, component_id: str, sha256: str) -> None:
        async with self._actions:
            record = self._record(component_id, sha256)
            _manifest, manager, request = self._runtime(record)
            operation = InstallOperation(component_id, self._log(component_id))
            await asyncio.to_thread(manager.ensure, request, operation)

    def _manifests(self, selected: dict[str, str]) -> dict[str, ComponentManifest]:
        return {name: validate_component(self._payload(self._record(name, digest).sha256))
                for name, digest in selected.items()}

    def validate_selection(self, selected: dict[str, str]) -> None:
        manifests = self._manifests(selected)
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ConflictError(f'Component dependency cycle includes {name}')
            if name in visited:
                return
            visiting.add(name)
            for required in manifests[name].requires:
                provider = manifests.get(required.component_id)
                if provider is None:
                    raise ConflictError(f'{name} requires selected component {required.component_id}')
                versions = {entry.protocol_id: entry.version for entry in provider.provides}
                version = versions.get(required.protocol_id)
                if version is None or Version(version) not in SpecifierSet(required.versions):
                    raise ConflictError(f'{name} requires {required.component_id} protocol '
                                        f'{required.protocol_id}{required.versions}; provided {version}')
                visit(required.component_id)
            visiting.remove(name)
            visited.add(name)

        for name in manifests:
            visit(name)

    async def select(self, component_id: str, sha256: str) -> None:
        async with self._actions:
            self._record(component_id, sha256)
            self._require_stopped(component_id)
            selected = {**self.state.selected, component_id: sha256}
            self.validate_selection(selected)
            self._save(ComponentState(records=self.state.records, selected=selected))

    async def select_all(self, selected: dict[str, str]) -> None:
        async with self._actions:
            for name in set(self.state.selected) | selected.keys():
                self._require_stopped(name)
            self.validate_selection(selected)
            self._save(ComponentState(records=self.state.records, selected=dict(selected)))

    async def update(self, component_id: str, sha256: str) -> None:
        async with self._actions:
            record = self._record(component_id, sha256)
            previous = self._record(component_id)
            self._require_stopped(component_id, allow_self=True)
            selected = {**self.state.selected, component_id: sha256}
            self.validate_selection(selected)
            _manifest, manager, request = self._runtime(record)
            await asyncio.to_thread(manager.ensure, request, InstallOperation(component_id, self._log(component_id)))
            active = self.running.get(component_id)
            restart = active is not None and active.process.returncode is None
            await self._stop(component_id)
            self._save(ComponentState(records=self.state.records, selected=selected))
            started: list[str] = []
            try:
                if restart:
                    await self._start(component_id, started)
            except (OSError, ValueError, ConflictError, ServiceUnavailableError, asyncio.CancelledError):
                logger.exception('Component update failed; restoring %s %s', component_id, previous.version)
                for name in reversed(started):
                    await self._stop(name)
                restored = {**self.state.selected, component_id: previous.sha256}
                self._save(ComponentState(records=self.state.records, selected=restored))
                if restart:
                    await self._start(component_id, [])
                raise

    def _require_stopped(self, component_id: str, *, allow_self: bool = False) -> None:
        affected = {component_id}
        manifests = self._manifests(self.state.selected)
        changed = True
        while changed:
            changed = False
            for name, manifest in manifests.items():
                if name not in affected and any(item.component_id in affected for item in manifest.requires):
                    affected.add(name)
                    changed = True
        for name in affected:
            if allow_self and name == component_id:
                continue
            process = self.running.get(name)
            if process is not None and process.process.returncode is None:
                raise ConflictError(f'Stop running component {name} before changing {component_id}')

    def endpoints(self, manifest: ComponentManifest) -> tuple[ComponentEndpoint, ...]:
        configured = self.configuration.get(manifest.component_id, {})
        return tuple(ComponentEndpoint(name=item.name, url=configured.get(item.name, item.url))
                     for item in manifest.endpoints)

    async def configure(self, component_id: str, endpoints: dict[str, str]) -> None:
        async with self._actions:
            self._require_stopped(component_id)
            manifest = validate_component(self._payload(self._record(component_id).sha256))
            if endpoints.keys() - {item.name for item in manifest.endpoints}:
                raise InvalidRequestError('Configuration references an undeclared endpoint')
            for url in endpoints.values():
                parsed = urllib.parse.urlsplit(url)
                if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                        or not parsed.port or parsed.username or parsed.password or parsed.query
                        or parsed.fragment or parsed.path not in {'', '/'}):
                    raise InvalidRequestError('Component endpoint requires a loopback HTTP port')
            configuration = {**self.configuration, component_id: dict(endpoints)}
            path = self.root / 'configuration.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(msgspec.json.encode(configuration))
            temporary.replace(path)
            self.configuration = configuration

    def _expand(self, manifest: ComponentManifest, payload: Path, values: tuple[str, ...]) -> tuple[str, ...]:
        substitutions = {'${F8_COMPONENT_ROOT}': str(payload), '${F8_DATA_ROOT}': str(self.data_dir)}
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
                raise InvalidRequestError(f'Unresolved component argument: {argument}')
            result.append(value)
        return tuple(result)

    async def start(self, component_id: str) -> None:
        async with self._actions:
            self.validate_selection(self.state.selected)
            started: list[str] = []
            try:
                await self._start(component_id, started)
            except (OSError, ValueError, ConflictError, ServiceUnavailableError, httpx.HTTPError, asyncio.CancelledError):
                logger.exception('Component startup failed: %s', component_id)
                for name in reversed(started):
                    await self._stop(name)
                raise

    async def _start(self, component_id: str, started: list[str]) -> None:
        record = self._record(component_id)
        manifest, manager, request = self._runtime(record)
        active = self.running.get(component_id)
        if active is not None and active.process.returncode is None:
            await self._health(manifest, active.process, active.instance)
            return
        if active is not None:
            await self._stop(component_id)
        for dependency in manifest.requires:
            await self._start(dependency.component_id, started)
        plan = manager.plan(request)
        if not manager.ready(plan.environment_id):
            raise ConflictError(f'Prepare {component_id} before starting it')
        executable, arguments = manager.python_launch(plan, manifest.launch.environment)
        log_path = self._log(component_id)
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
                     'F8_COMPONENT_ROOT': str(self._payload(record.sha256)), 'F8_COMPONENT_INSTANCE': instance,
                     'F8_SERVICE_INDEX': str(self.data_dir / 'distribution/config/service-index.json'),
                     **dict(zip(manifest.launch.env, self._expand(manifest, self._payload(record.sha256),
                                                                 tuple(manifest.launch.env.values())), strict=True))},
                start_new_session=os.name != 'nt',
            )
        except OSError:
            log.close()
            raise
        self.running[component_id] = RunningComponent(process, log, instance)
        started.append(component_id)
        await self._health(manifest, process, instance)

    async def _health(self, manifest: ComponentManifest, process: asyncio.subprocess.Process, instance: str) -> None:
        probe = manifest.health
        endpoint = next(item.url for item in self.endpoints(manifest) if item.name == probe.endpoint)
        deadline = asyncio.get_running_loop().time() + probe.timeout_seconds
        last_error = 'No health response'
        bootstrapped = probe.bootstrap_path is None
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            while asyncio.get_running_loop().time() < deadline:
                if process.returncode is not None:
                    raise ServiceUnavailableError(f'{manifest.component_id} exited ({process.returncode}); '
                                                  f'log: {self._log(manifest.component_id)}')
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
                            or data.get('componentInstance', data.get('component_instance')) != instance
                            or data.get('version') != manifest.version):
                        raise ServiceUnavailableError(f'{manifest.component_id} health identity/protocol mismatch')
                    if data.get('status') == 'ok':
                        return
                    last_error = f'Unexpected health status: {data.get("status")}'
                await asyncio.sleep(0.1)
        raise ServiceUnavailableError(f'{manifest.component_id} startup timed out: {last_error}; '
                                      f'log: {self._log(manifest.component_id)}')

    async def stop(self, component_id: str) -> None:
        async with self._actions:
            dependents = [name for name, manifest in self._manifests(self.state.selected).items()
                          if name != component_id and any(item.component_id == component_id for item in manifest.requires)
                          and name in self.running and self.running[name].process.returncode is None]
            if dependents:
                raise ConflictError(f'Stop dependent components first: {", ".join(dependents)}')
            await self._stop(component_id)

    async def _stop(self, component_id: str) -> None:
        active = self.running.pop(component_id, None)
        if active is None:
            return
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

    async def uninstall(self, component_id: str, sha256: str) -> None:
        async with self._actions:
            self._record(component_id, sha256)
            self._require_stopped(component_id)
            selected = dict(self.state.selected)
            if selected.get(component_id) == sha256:
                del selected[component_id]
                self.validate_selection(selected)
            records = tuple(record for record in self.state.records if record.sha256 != sha256)
            self._save(ComponentState(records=records, selected=selected))
            # Keep immutable payload/cache files and prefixes available for rollback/reimport.

    async def close(self) -> None:
        async with self._actions:
            for name in reversed(tuple(self.running)):
                await self._stop(name)
