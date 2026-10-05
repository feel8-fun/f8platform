from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from collections.abc import Generator
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import tomllib
from threading import RLock
from typing import Literal
import zipfile

import msgspec
import packaging
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
import yaml

from f8pysdk._specs.builtin_fields import normalize_describe_payload_dict
from f8pysdk.extension_capabilities import validate_capabilities
from f8pysdk.extension_spec import ExtensionTool
from f8pysdk.service_paths import ServicePaths

from f8pysdk.codec import copy_model, validate_as
from f8pysdk.monitoring import validate_describe_monitor_contract
from f8pysdk.resource_paths import model_root as configured_model_root
from f8pysdk.service_runtime_tools.inventory import ServiceCatalog
from f8pysdk.service_runtime_tools.inventory.index import (
    IndexedService, ServiceIndex, default_service_index, index_paths, indexed_entry,
    load_index_into_catalog, read_service_index,
)
from f8pysdk.specs import F8ServiceDescribe, F8ServiceEntry

from .environments import EnvironmentManager
from .runtime_sources import read_runtime_catalog
from .runtime_registry import RuntimeRegistry, RuntimeSourcesSnapshot
from .extension_artifacts import prepare_artifact
from .errors import ConflictError, InvalidRequestError, NotFoundError
from .extension_models import (
    ExtensionDetail, ExtensionServiceDetail, ExtensionSkillDetail,
    EnvironmentStatus, ExtensionCatalog, ExtensionImportRequest, ExtensionInstallPlan, ExtensionManifest,
    ExtensionRecord, ExtensionStatus, RuntimeStorageStatus,
)
from .extension_operation import ExtensionInstallCancelled, InstallOperation
from f8pysdk.release_spec import BundledExtensionCatalog, PublishedArtifact

from .shared_dependencies import RuntimeProbe, validate_shared_package


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtensionPayload:
    root: Path
    index_path: Path
    index: ServiceIndex
    environments: EnvironmentManager
    source_checkout: bool
    release_sha256: str | None


@dataclass(frozen=True)
class ExtensionSourcesSnapshot:
    manifests: dict[str, ExtensionManifest]
    payloads: dict[str, ExtensionPayload]
    services: dict[str, IndexedService]
    owners: dict[str, str]
    preinstalled: set[str]
    failures: dict[str, str]
    details: dict[str, str]
    runtimes: RuntimeSourcesSnapshot


def bundled_extension_paths(root: Path) -> tuple[Path, ...]:
    path = root / 'config/extension-packages.json'
    if not path.is_file():
        return ()
    catalog = msgspec.json.decode(path.read_bytes(), type=BundledExtensionCatalog)
    paths: list[Path] = []
    for item in catalog.packages:
        prefix = '${F8_PACKAGE_ROOT}/'
        if not re.fullmatch(r'[0-9a-f]{64}', item.sha256) or not item.path.startswith(prefix):
            raise ValueError('Invalid bundled extension package reference')
        source = (root / item.path.removeprefix(prefix)).resolve()
        if not source.is_relative_to(root.resolve()) or source.name != item.sha256:
            raise ValueError('Bundled extension package must use a contained content-addressed path')
        paths.append(source)
    return tuple(paths)


class ExtensionManager:
    def __init__(self, data_dir: Path, *, base_index: Path | None = None) -> None:
        self._root = data_dir / 'extensions'
        self._data_dir = data_dir
        self._base_index = (base_index or default_service_index()).resolve()
        base = (read_service_index(self._base_index) if self._base_index.is_file() else
                ServiceIndex(schemaVersion='f8serviceIndex/1', services=(), modelRoot='${F8_MODEL_ROOT}'))
        self._source_root = index_paths(self._base_index, base).package_root
        self.environments = EnvironmentManager(
            data_dir, self._source_root, runtime_catalog_path=self._base_index.with_name('runtime-environments.json'),
        )
        self._tool_running: Callable[[str], bool] = lambda _extension_id: False
        self._lock = RLock()
        self._actions = asyncio.Lock()
        self._operation: InstallOperation | None = None
        self._task: asyncio.Task[None] | None = None
        self._environment_refresh_task: asyncio.Task[None] | None = None
        self._failures: dict[str, str] = {}
        self._details: dict[str, str] = {}
        self._state_path = self._root / 'state.json'
        self._sources_path = self._root / 'sources.json'
        self._source_digests: list[str] = []
        self._payloads: dict[str, ExtensionPayload] = {}
        self._records: dict[str, ExtensionRecord] = {}
        self.runtime_registry = RuntimeRegistry(data_dir, self.environments)
        self._manifests: dict[str, ExtensionManifest] = {}
        self._services: dict[str, IndexedService] = {}
        self._owners: dict[str, str] = {}
        self._preinstalled: set[str] = set()
        catalog_path = self._base_index.with_name('extensions.json')
        self.has_catalog = catalog_path.is_file()
        if not self.has_catalog:
            return
        self._add_catalog(self._source_root, preinstalled=True, index_path=self._base_index)
        for root in bundled_extension_paths(self._source_root):
            self._add_catalog(root, preinstalled=False, release_sha256=root.name)
        if self._sources_path.is_file():
            try:
                self._source_digests = msgspec.json.decode(self._sources_path.read_bytes(), type=list[str])
            except (OSError, msgspec.DecodeError):
                logger.warning('Cannot read extension sources; using the bundled catalog', exc_info=True)
            for digest in self._source_digests:
                if not re.fullmatch(r'[0-9a-f]{64}', digest):
                    logger.warning('Ignoring invalid extension source digest: %r', digest)
                    continue
                snapshot = self._snapshot_sources()
                try:
                    self._add_catalog(self._root / 'payloads' / digest, preinstalled=False, replace_existing=True, restoring=True,
                                      release_sha256=digest)
                except (OSError, ValueError, msgspec.DecodeError, ConflictError):
                    self._restore_sources(snapshot)
                    logger.exception('Cannot load imported extension source %s', digest)
        state_readable = True
        if self._state_path.is_file():
            try:
                self._records = msgspec.json.decode(self._state_path.read_bytes(), type=dict[str, ExtensionRecord])
            except (OSError, msgspec.DecodeError):
                logger.warning('Cannot read extension installation state; using the distribution preset', exc_info=True)
                state_readable = False
            unknown = set(self._records) - self._manifests.keys()
            if unknown:
                logger.warning('Installed extensions absent from this distribution: %s', sorted(unknown))
        for declared in self._manifests.values():
            manifest = self._manifest(declared.extension_id)
            record = self._records.get(manifest.extension_id)
            if record is None and manifest.extension_id in self._preinstalled and self._supported(manifest):
                plan = self._payloads[manifest.extension_id].environments.plan(manifest)
                record = ExtensionRecord(version=manifest.version, installed=True, enabled=True,
                                         environment_id=plan.environment_id)
                self._records[manifest.extension_id] = record
            if record is not None and record.installed:
                original_id = record.environment_id or ''
                canonical_id = self.runtime_registry.canonical_id(original_id)
                source = self.runtime_registry.sources.get(canonical_id)
                if (source is not None and original_id == source.target.manager.legacy_environment_id(source.target.environment)
                        and source.target.manager.can_reuse_development_environment(source.target.environment)):
                    record = copy_model(record, update={'environment_id': canonical_id})
                    self._records[manifest.extension_id] = record
                if record.version != manifest.version:
                    logger.warning('Extension %s changed from %s to %s; explicit reinstall is required',
                                   manifest.extension_id, record.version, manifest.version)
                    self._records[manifest.extension_id] = copy_model(record, update={'installed': False, 'enabled': False})
                    continue
                payload = self._payloads[manifest.extension_id]
                if payload.root == self._source_root and not (payload.root / 'config/artifact.json').is_file():
                    descriptions = (
                        index_paths(payload.index_path, payload.index, self._services[name]).package_path(
                            self._services[name].describe, relative_to=payload.index_path.parent,
                        ) for name in manifest.service_classes
                    )
                    missing = tuple(str(path) for path in descriptions if not path.is_file())
                    if missing:
                        detail = 'Build or prepare this extension; missing descriptions: ' + ', '.join(missing)
                        logger.info('Development extension %s is not ready: %s', manifest.extension_id, detail)
                        self._records[manifest.extension_id] = copy_model(record, update={'installed': False, 'enabled': False})
                        self._failures[manifest.extension_id] = detail
                        continue
                try:
                    if manifest.runtime.kind == 'shared' and self._registration(manifest.extension_id).is_file():
                        environment = manifest.runtime.environment
                        assert environment is not None
                        target = self.environments.shared_targets.get(environment)
                        expected = target.plan if target is not None else self.environments.preset_plan(environment)
                        if record.environment_id != expected.environment_id or not payload.environments.ready(expected.environment_id):
                            continue
                    plan = payload.environments.plan(manifest)
                    if record.environment_id != plan.environment_id and self._registration(manifest.extension_id).is_file():
                        # Keep the verified existing registration until explicit
                        # preparation reconciles it with the new definition.
                        # Its payload may have moved or been cleaned since the
                        # last launch. Never expose an unreadable retained index.
                        load_index_into_catalog(path=self._registration(manifest.extension_id), catalog=ServiceCatalog())
                        continue
                    self._copy_model_metadata(manifest)
                    self._write_registration(manifest, record)
                    load_index_into_catalog(path=self._registration(manifest.extension_id), catalog=ServiceCatalog())
                except (OSError, ValueError, msgspec.DecodeError) as exc:
                    logger.exception('Cannot restore extension %s', manifest.extension_id)
                    self._records[manifest.extension_id] = copy_model(record, update={'installed': False, 'enabled': False})
                    self._failures[manifest.extension_id] = (
                        f'{type(exc).__name__}: {exc}; reinstall this extension to restore its registration'
                    )
        if state_readable:
            self._save_records()

    def _add_catalog(self, root: Path, *, preinstalled: bool, replace_existing: bool = False, restoring: bool = False,
                     index_path: Path | None = None, release_sha256: str | None = None) -> tuple[str, ...]:
        root = root.resolve()
        index_path = index_path or root / 'config/service-index.json'
        catalog = msgspec.json.decode(index_path.with_name('extensions.json').read_bytes(), type=ExtensionCatalog)
        index = (read_service_index(index_path) if index_path.is_file() else
                 ServiceIndex(schemaVersion='f8serviceIndex/1', services=(), modelRoot='${F8_MODEL_ROOT}'))
        services = {item.serviceClass: item for item in index.services}
        owners = self._validate_catalog(catalog, services, root=root)
        descriptor_path = root / 'config/artifact.json'
        source_path = index_path.with_name('extension-sources.json')
        development_sources = (msgspec.json.decode(source_path.read_bytes(), type=dict[str, str])
                               if preinstalled and source_path.is_file() else {})
        if development_sources and descriptor_path.is_file():
            raise ValueError('Published artifacts cannot declare development source checkouts')
        if set(development_sources) - {manifest.extension_id for manifest in catalog.extensions}:
            raise ValueError('Development sources must belong to declared extensions')
        if descriptor_path.is_file():
            descriptor = msgspec.json.decode(descriptor_path.read_bytes(), type=PublishedArtifact)
            if descriptor.kind != 'extension' or len(catalog.extensions) != 1:
                raise ValueError('Published extension artifact must own exactly one extension')
            manifest = catalog.extensions[0]
            if (descriptor.artifact_id, descriptor.version) != (manifest.extension_id, manifest.version):
                raise ValueError('Extension artifact identity disagrees with catalog')
            platform = 'windows-x86_64' if sys.platform == 'win32' else 'linux-x86_64'
            if descriptor.platform not in {'any', platform}:
                raise ValueError('Extension artifact is incompatible with this platform')
        if not preinstalled and any(manifest.runtime.kind not in {'native', 'pixi', 'shared'} for manifest in catalog.extensions):
            raise ValueError('Published extensions must declare a native, shared, or locked Pixi runtime')
        if not preinstalled and not catalog.extensions:
            raise ValueError('Published extension catalog must not be empty')
        if not preinstalled and len(catalog.extensions) != 1:
            raise ValueError('An extension package must own exactly one extension')
        runtime_catalog = read_runtime_catalog(root, catalog_path=index_path.with_name('runtime-environments.json'))
        if not preinstalled and runtime_catalog.runtimes:
            manifest = catalog.extensions[0]
            if (manifest.runtime.kind != 'pixi'
                    or manifest.runtime.environment not in {item.runtime_id for item in runtime_catalog.runtimes}):
                raise ValueError('Extension default environment must be declared in its workspace')
        elif not preinstalled and catalog.extensions[0].runtime.kind == 'pixi':
            manifest = catalog.extensions[0]
            definition = tomllib.loads((root / 'pixi.toml').read_text(encoding='utf-8'))
            declared = definition.get('environments', {})
            if manifest.runtime.environment not in declared:
                raise ValueError('Extension default environment must be declared in its workspace')
        conflicts = set(services) & self._services.keys()
        ids = {manifest.extension_id for manifest in catalog.extensions}
        replaced = ids & self._manifests.keys()
        if conflicts and any(self._owners[name] not in ids for name in conflicts):
            raise ConflictError('Extension package conflicts with another extension service class')
        if replaced and not replace_existing:
            raise ConflictError('Extension package conflicts with an existing extension ID or service class')
        if replaced and all(self._payloads[identifier].root == root for identifier in ids if identifier in self._payloads) and ids == replaced:
            return tuple(manifest.extension_id for manifest in catalog.extensions)
        for manifest in catalog.extensions:
            if manifest.extension_id not in replaced:
                continue
            record = self._records.get(manifest.extension_id)
            if record is not None and record.installed:
                raise ConflictError('Uninstall the current extension before importing another version')
            if not restoring and self._manifests[manifest.extension_id].version == manifest.version:
                raise ConflictError('Different extension contents must use a new version')
        for item in index.services:
            paths = index_paths(index_path, index, item)
            for relative in (*item.manifests.values(), item.describe):
                path = paths.package_path(relative, relative_to=index_path.parent)
                # Development catalogs can precede native builds. Keep their
                # metadata available; registration checks readiness per extension.
                missing_development_describe = (
                    preinstalled and not descriptor_path.is_file() and relative == item.describe
                )
                if not path.is_relative_to(root) or (not path.is_file() and not missing_development_describe):
                    raise ValueError(f'Missing or unsafe payload path for {item.serviceClass}: {relative}')
        environments = self.environments if root == self._source_root else EnvironmentManager(
            self._data_dir, root, official=self.environments.official,
        )
        # Keep old runtime sources: other installed extensions may still bind to
        # their immutable environment IDs, and replay reconstructs those bindings.
        for identifier in replaced:
            old = self._manifests[identifier]
            for name in old.service_classes:
                self._services.pop(name)
                self._owners.pop(name)
            self._preinstalled.discard(identifier)
            self._failures.pop(identifier, None)
            self._details.pop(identifier, None)
        for manifest in catalog.extensions:
            self._manifests[manifest.extension_id] = manifest
            reference = development_sources.get(manifest.extension_id)
            owner = environments
            if reference is not None:
                source = ServicePaths.for_index(root / 'config/service-index.json').package_path(reference, relative_to=root)
                if not source.is_dir() or source == root:
                    raise ValueError(f'Invalid development checkout for {manifest.extension_id}')
                owner = EnvironmentManager(self._data_dir, source, official=self.environments)
            source_checkout = reference is not None or (preinstalled and not descriptor_path.is_file() and release_sha256 is None)
            self._payloads[manifest.extension_id] = ExtensionPayload(root, index_path, index, owner, source_checkout, release_sha256)
            self.runtime_registry.add_source(owner, manifest, official=preinstalled and reference is None)
        self._services.update(services)
        self._owners.update(owners)
        if preinstalled:
            self._preinstalled.update(catalog.preinstalled)
        return tuple(manifest.extension_id for manifest in catalog.extensions)

    def _validate_catalog(self, catalog: ExtensionCatalog, services: dict[str, IndexedService], *, root: Path) -> dict[str, str]:
        ids: set[str] = set()
        owners: dict[str, str] = {}
        for manifest in catalog.extensions:
            if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', manifest.extension_id):
                raise ValueError(f'Invalid extension ID: {manifest.extension_id!r}')
            if manifest.extension_id in ids or not manifest.version or not (manifest.service_classes or manifest.tools or manifest.skills or manifest.resources or manifest.application is not None):
                raise ValueError(f'Duplicate or invalid extension: {manifest.extension_id}')
            ids.add(manifest.extension_id)
            validate_capabilities(manifest, ServicePaths.for_index(root / 'config/service-index.json'))
            runtime = manifest.runtime
            if runtime.kind in {'pixi', 'workspace', 'shared'} and not runtime.environment:
                raise ValueError(f'Missing environment for {manifest.extension_id}')
            if runtime.kind == 'shared':
                if runtime.provider_version is not None:
                    SpecifierSet(runtime.provider_version)
                if runtime.provider_version is not None and runtime.provider_id is None:
                    raise ValueError('Runtime providerVersion requires providerId')
                if runtime.requires_python is not None:
                    SpecifierSet(runtime.requires_python)
                for dependency in runtime.dependencies:
                    requirement = Requirement(dependency)
                    if requirement.url is not None:
                        raise ValueError(f'Shared extensions cannot declare URL dependencies: {dependency}')
            elif (runtime.requires_python is not None or runtime.dependencies or runtime.provider_id is not None
                  or runtime.provider_version is not None or runtime.abi is not None):
                raise ValueError(f'Only shared extensions can declare official runtime requirements: {manifest.extension_id}')
            if runtime.environment is not None and not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', runtime.environment):
                raise ValueError(f'Invalid environment name for {manifest.extension_id}')
            for directory in manifest.model_directories:
                if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', directory):
                    raise ValueError(f'Invalid model directory for {manifest.extension_id}')
            for service_class in manifest.service_classes:
                if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', service_class):
                    raise ValueError(f'Invalid service class for {manifest.extension_id}')
                if service_class not in services:
                    raise ValueError(f'Missing service {service_class} for {manifest.extension_id}')
                if service_class in owners:
                    raise ValueError(f'Service {service_class} has multiple extension owners')
                owners[service_class] = manifest.extension_id
        if set(catalog.preinstalled) - ids:
            raise ValueError('Preinstalled extensions must be declared in the catalog')
        if set(services) - owners.keys():
            raise ValueError(f'Services without an extension owner: {sorted(set(services) - owners.keys())}')
        return owners

    def _manifest(self, extension_id: str) -> ExtensionManifest:
        manifest = self._manifests.get(extension_id)
        if manifest is None:
            raise NotFoundError(f'Unknown extension: {extension_id}')
        return manifest

    def manifest(self, extension_id: str) -> ExtensionManifest:
        return self._manifest(extension_id)

    def detail(self, extension_id: str) -> ExtensionDetail:
        """Read package metadata without activating or launching its capabilities."""
        with self._lock:
            manifest = self._manifest(extension_id)
            payload = self._payloads[extension_id]
            services: list[ExtensionServiceDetail] = []
            for service_class in manifest.service_classes:
                item = self._services[service_class]
                path = index_paths(payload.index_path, payload.index, item).resolve(
                    item.describe, relative_to=payload.index_path.parent,
                )
                describe: F8ServiceDescribe | None = None
                if path.is_file():
                    raw = msgspec.json.decode(path.read_bytes(), type=dict[str, object])
                    describe = validate_as(F8ServiceDescribe, normalize_describe_payload_dict(raw))
                    if describe.service.serviceClass != service_class:
                        raise InvalidRequestError(f'Service description class mismatch: {service_class}')
                services.append(ExtensionServiceDetail(service_class=service_class, describe=describe))
            paths = index_paths(payload.index_path, payload.index)
            skills = tuple(ExtensionSkillDetail(
                skill_id=skill.skill_id,
                content=paths.package_path(skill.path, relative_to=payload.root).read_text(encoding='utf-8'),
            ) for skill in manifest.skills)
            return ExtensionDetail(extension_id=extension_id, services=tuple(services),
                                   tools=manifest.tools, skills=skills)

    def _supported(self, manifest: ExtensionManifest) -> bool:
        if any(tool.platforms and sys.platform not in tool.platforms for tool in manifest.tools):
            return False
        if manifest.tools and manifest.runtime.kind == 'native':
            payload = self._payloads[manifest.extension_id]
            paths = index_paths(payload.index_path, payload.index)
            if any(not paths.package_path(tool.command, relative_to=paths.package_path(tool.workdir, relative_to=paths.package_root)).is_file() for tool in manifest.tools):
                return False
        return all(sys.platform in self._services[name].manifests or 'any' in self._services[name].manifests
                   for name in manifest.service_classes)

    def set_tool_running_probe(self, probe: Callable[[str], bool]) -> None:
        self._tool_running = probe

    def active_manifests(self) -> tuple[ExtensionManifest, ...]:
        with self._lock:
            return tuple(manifest for manifest in self._manifests.values()
                         if self.status(manifest.extension_id).state == 'installed')

    def active_skill_files(self) -> dict[str, Path]:
        return {f'{manifest.extension_id}:{skill.skill_id}': self.capability_file(manifest.extension_id, skill.path)
                for manifest in self.active_manifests() for skill in manifest.skills}

    def capability_file(self, extension_id: str, reference: str) -> Path:
        if self.status(extension_id).state != 'installed':
            raise InvalidRequestError('Extension must be installed and enabled')
        payload = self._payloads[extension_id]
        return index_paths(payload.index_path, payload.index).package_path(reference, relative_to=payload.root)

    def tool_launcher(self, extension_id: str, tool_id: str) -> tuple[ExtensionTool, list[str], Path, dict[str, str]]:
        if self.status(extension_id).state != 'installed':
            raise InvalidRequestError('Extension must be installed and enabled')
        manifest = self._manifest(extension_id)
        tool = next((item for item in manifest.tools if item.tool_id == tool_id), None)
        if tool is None:
            raise NotFoundError(f'Unknown extension tool: {extension_id}/{tool_id}')
        payload = self._payloads[extension_id]
        paths = index_paths(payload.index_path, payload.index)
        cwd = paths.package_path(tool.workdir, relative_to=payload.root)
        args = [str(paths.resolve(arg, relative_to=cwd)) if '${' in arg else arg for arg in tool.args]
        if manifest.runtime.kind == 'native':
            command = [str(paths.package_path(tool.command, relative_to=cwd)), *args]
        else:
            if tool.command != 'python':
                raise InvalidRequestError('Managed tool entrypoints must declare python')
            plan = payload.environments.plan(manifest)
            environment = manifest.runtime.environment or ('studio-runtime' if manifest.runtime.kind == 'bundled' else None)
            if environment is None or not payload.environments.ready(plan.environment_id):
                raise InvalidRequestError('Tool runtime is unavailable; reinstall the extension')
            executable, prefix = payload.environments.python_launch(plan, environment)
            command = [executable, *prefix, *args]
            if manifest.runtime.kind == 'shared':
                if len(args) != 2 or args[0] != '-m':
                    raise InvalidRequestError('Shared tools must launch python -m module')
                code = self._registration(extension_id).parent / 'python'
                command = [executable, *prefix, str(Path(__file__).with_name('_shared_entrypoint.py')),
                           str(code), args[1]]
        return tool, command, cwd, paths.environment()

    def _registration(self, extension_id: str) -> Path:
        return self._root / 'registrations' / extension_id / 'service-index.json'

    def _save_records(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix('.tmp')
        temporary.write_bytes(msgspec.json.encode(self._records))
        temporary.replace(self._state_path)

    def _commit_record(self, extension_id: str, record: ExtensionRecord | None) -> None:
        with self._lock:
            previous = self._records.get(extension_id)
            if record is None:
                self._records.pop(extension_id, None)
            else:
                self._records[extension_id] = record
            try:
                self._save_records()
            except OSError:
                if previous is None:
                    self._records.pop(extension_id, None)
                else:
                    self._records[extension_id] = previous
                logger.exception('Cannot persist extension state for %s', extension_id)
                raise

    def active_indexes(self) -> tuple[Path, ...]:
        with self._lock:
            return tuple(self._registration(extension_id) for extension_id in self._manifests
                         if (record := self._records.get(extension_id)) is not None
                         and record.installed and record.enabled and extension_id not in self._failures)

    async def _refresh_record(self, extension_id: str, record: ExtensionRecord,
                              refresh: Callable[[], object]) -> None:
        previous = self._records.get(extension_id)
        self._commit_record(extension_id, record)
        try:
            await asyncio.to_thread(refresh)
        except Exception:
            logger.exception('Cannot refresh catalog after updating extension %s', extension_id)
            self._commit_record(extension_id, previous)
            raise

    def statuses(self) -> tuple[ExtensionStatus, ...]:
        return tuple(self.status(extension_id) for extension_id in self._manifests)

    def status(self, extension_id: str) -> ExtensionStatus:
        manifest = self._manifest(extension_id)
        with self._lock:
            record = self._records.get(extension_id)
            detail = self._details.get(extension_id, '')
            state: Literal['unavailable', 'available', 'installing', 'installed', 'disabled', 'failed']
            if self._operation is not None and self._operation.extension_id == extension_id:
                state, detail = 'installing', self._operation.detail
            elif extension_id in self._failures:
                state, detail = 'failed', self._failures[extension_id]
            elif not self._supported(manifest):
                state, detail = 'unavailable', 'This extension does not support this platform.'
            elif record is not None and record.installed:
                state = 'installed' if record.enabled else 'disabled'
            else:
                state = 'available'
            return ExtensionStatus(
                extension_id=extension_id, name=manifest.name, version=manifest.version,
                description=manifest.description, state=state, detail=detail,
                service_classes=manifest.service_classes, runtime_kind=manifest.runtime.kind,
                environment_id=record.environment_id if record is not None and record.installed else None,
                preinstalled=extension_id in self._preinstalled,
                tool_ids=tuple(tool.tool_id for tool in manifest.tools),
                skill_ids=tuple(skill.skill_id for skill in manifest.skills),
                resource_ids=tuple(resource.resource_id for resource in manifest.resources),
                runtime_environment=manifest.runtime.environment,
                source_checkout=self._payloads[extension_id].source_checkout,
                source_path=str(self._payloads[extension_id].environments.source_root) if self._payloads[extension_id].source_checkout else None,
                release_sha256=self._payloads[extension_id].release_sha256,
            )

    def install_plan(self, extension_id: str) -> ExtensionInstallPlan:
        manifest = self._manifest(extension_id)
        if not self._supported(manifest):
            raise InvalidRequestError('This extension does not support this platform')
        for name in manifest.service_classes:
            self._entry(manifest, name)
        return self._payloads[extension_id].environments.plan(manifest)


    def _entry(self, manifest: ExtensionManifest, service_class: str) -> F8ServiceEntry:
        payload = self._payloads[manifest.extension_id]
        entry = indexed_entry(payload.index_path, payload.index, self._services[service_class])
        if entry is None:
            raise InvalidRequestError(f'Missing launcher for {service_class}')
        args = entry.launch.args or []
        if manifest.runtime.kind == 'pixi' and entry.launch.command in {'python', 'python.exe'}:
            if len(args) != 2 or args[0] != '-m' or not re.fullmatch(r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*', args[1]):
                raise InvalidRequestError('Independent Python services must launch python -m module')
        elif manifest.runtime.kind in {'pixi', 'workspace'}:
            if (Path(entry.launch.command).name not in {'pixi', 'pixi.exe'} or len(args) != 4
                    or args[:3] != ['run', '-e', manifest.runtime.environment]):
                raise InvalidRequestError(f'Service {service_class} must explicitly use the declared extension environment')
        elif manifest.runtime.kind == 'shared':
            if (entry.launch.command not in {'python', 'python.exe'} or len(args) < 2 or args[0] != '-m'
                    or not re.fullmatch(r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*', args[1])):
                raise InvalidRequestError(f'Shared service {service_class} must launch an explicit Python module')
            source = payload.root / 'python'
            module_path = source.joinpath(*args[1].split('.'))
            if not source.resolve().is_relative_to(payload.root):
                raise InvalidRequestError('Shared extension Python directory escapes its payload')
            if not module_path.with_suffix('.py').is_file() and not (module_path / '__main__.py').is_file():
                raise InvalidRequestError(f'Missing Python entrypoint for {service_class}: {args[1]}')
        return entry

    def environment_statuses(self) -> tuple[EnvironmentStatus, ...]:
        with self._lock:
            sources = self.runtime_registry.source_snapshot()
            ids = list(sources)
            result: list[EnvironmentStatus] = []
            for identifier in ids:
                status = self.runtime_registry.status(identifier)
                source = sources.get(identifier)
                users: list[str] = []
                changed = False
                for extension_id, original in self._manifests.items():
                    manifest = self._manifest(extension_id)
                    record = self._records.get(extension_id)
                    owns = (source is not None and original.runtime.kind not in {'native', 'shared'}
                            and source.target.manager is self._payloads[extension_id].environments.for_environment(original.runtime.environment or 'studio-runtime')
                            and source.target.environment == (original.runtime.environment or ('studio-runtime' if original.runtime.kind == 'bundled' else None)))
                    shared = original.runtime.kind == 'shared' and (manifest.runtime.environment == identifier or (
                        source is not None and source.source == 'official' and source.name == manifest.runtime.environment))
                    if owns or shared:
                        users.append(extension_id)
                        if record is not None and record.installed and source is not None:
                            changed = changed or record.environment_id != source.target.plan.environment_id
                result.append(copy_model(status, update={
                    'extension_ids': tuple(sorted(users)),
                    'service_classes': tuple(name for extension_id in users for name in self._manifests[extension_id].service_classes),
                    'tool_ids': tuple(f'{extension_id}/{tool.tool_id}' for extension_id in users for tool in self._manifests[extension_id].tools),
                    'can_remove': status.runtime_kind == 'pixi' and identifier not in self._referenced_environments(),
                    'state': 'changed' if changed and status.state not in {'preparing', 'failed'} else status.state,
                    'detail': 'Installed extension records refer to an earlier environment definition. Prepare this environment to verify and update them.'
                    if changed and status.state not in {'preparing', 'failed'} else status.detail,
                }))
            return tuple(result)

    async def prepare_environment(self, identifier: str, refresh: Callable[[], object],
                                  is_running: Callable[[str], bool]) -> EnvironmentStatus:
        with self._lock:
            self._require_idle()
        environment = next((item for item in self.environment_statuses() if item.environment_id == identifier), None)
        if environment is None:
            raise NotFoundError(f'Unknown environment: {identifier}')
        if any(self._tool_running(extension_id) for extension_id in environment.extension_ids) or any(
            is_running(service_class) for service_class in environment.service_classes
        ):
            raise ConflictError('Stop services and tools using this environment before preparing it')
        status = await self.runtime_registry.prepare(identifier)
        task = self.runtime_registry.preparation_task
        assert task is not None
        async def reconcile() -> None:
            await task
            if self.runtime_registry.status(identifier).state != 'ready':
                return
            source = self.runtime_registry.source(identifier)
            canonical_id = self.runtime_registry.status(identifier).environment_id
            for current in self.environment_statuses():
                if current.environment_id != canonical_id:
                    continue
                for extension_id in current.extension_ids:
                    record = self._records.get(extension_id)
                    if record is None or not record.installed:
                        continue
                    updated = copy_model(record, update={'environment_id': source.target.plan.environment_id})
                    self._write_registration(self._manifest(extension_id), updated)
                    self._commit_record(extension_id, updated)
            await asyncio.to_thread(refresh)
        self._environment_refresh_task = asyncio.create_task(reconcile(), name='refresh-runtime-records')
        self._environment_refresh_task.add_done_callback(self._report_task_failure)
        return status



    def set_runtime_storage(self, path: str) -> RuntimeStorageStatus:
        self._require_idle()
        return self.runtime_registry.set_storage(path)

    def remove_environment(self, identifier: str) -> None:
        self._require_idle()
        self.runtime_registry.remove(identifier, self._referenced_environments())

    @contextmanager
    def runtime_maintenance(self) -> Generator[None]:
        with ExitStack() as scope:
            with self._lock:
                self._require_idle()
                scope.enter_context(self.runtime_registry.maintenance())
            yield

    def referenced_environment_ids(self) -> set[str]:
        with self._lock:
            return self._referenced_environments()

    @property
    def runtime_busy(self) -> bool:
        return self._operation is not None or self.runtime_registry.busy or (
            self._environment_refresh_task is not None and not self._environment_refresh_task.done())

    def clean_unused_environments(self, referenced: set[str]) -> None:
        with self.runtime_maintenance():
            self.runtime_registry.clean_unused_environments(referenced | self._referenced_environments())

    def _require_idle(self) -> None:
        if self._environment_refresh_task is not None and not self._environment_refresh_task.done():
            raise ConflictError('Environment references are being refreshed; wait for them to finish')
        if self.runtime_registry.busy:
            raise ConflictError('An environment is being prepared; wait for it to finish')
        if self._operation is not None:
            raise ConflictError(f'Extension operation is running: {self._operation.extension_id}')

    def service_enabled(self, service_class: str) -> bool:
        with self._lock:
            owner = self._owners.get(service_class)
            if owner is None:
                return True
            record = self._records.get(owner)
            return record is not None and record.installed and record.enabled and owner not in self._failures

    def _snapshot_sources(self) -> ExtensionSourcesSnapshot:
        return ExtensionSourcesSnapshot(manifests=dict(self._manifests), payloads=dict(self._payloads),
            services=dict(self._services), owners=dict(self._owners), preinstalled=set(self._preinstalled),
            failures=dict(self._failures), details=dict(self._details), runtimes=self.runtime_registry.snapshot_sources())

    def _restore_sources(self, snapshot: ExtensionSourcesSnapshot) -> None:
        self._manifests = snapshot.manifests
        self._payloads = snapshot.payloads
        self._services = snapshot.services
        self._owners = snapshot.owners
        self._preinstalled = snapshot.preinstalled
        self._failures = snapshot.failures
        self._details = snapshot.details
        self.runtime_registry.restore_sources(snapshot.runtimes)

    async def import_package(self, request: ExtensionImportRequest) -> tuple[ExtensionStatus, ...]:
        async with self._actions:
            with self._lock:
                self._require_idle()
            try:
                payload = await asyncio.to_thread(prepare_artifact, request, self._root)
                with self._lock:
                    self._require_idle()
                    snapshot = self._snapshot_sources()
                    committed = False
                    try:
                        added = self._add_catalog(payload, preinstalled=False, replace_existing=True, release_sha256=request.sha256)
                        if all(snapshot.payloads.get(identifier) is self._payloads[identifier] for identifier in added):
                            committed = True
                            return self.statuses()
                        sources = [*self._source_digests, request.sha256]
                        temporary = self._sources_path.with_suffix('.tmp')
                        temporary.write_bytes(msgspec.json.encode(sources))
                        temporary.replace(self._sources_path)
                        self._source_digests = sources
                        committed = True
                    finally:
                        if not committed:
                            self._restore_sources(snapshot)
            except (OSError, ValueError, msgspec.DecodeError, zipfile.BadZipFile) as exc:
                logger.exception('Cannot import extension package from %s', request.url)
                raise InvalidRequestError(f'Cannot import extension package: {exc}') from exc
            return self.statuses()

    async def install(self, extension_id: str, refresh: Callable[[], object]) -> ExtensionStatus:
        async with self._actions:
            await asyncio.to_thread(self.install_plan, extension_id)
            with self._lock:
                self._require_idle()
                record = self._records.get(extension_id)
                if record is not None and record.installed and extension_id not in self._failures:
                    return self.status(extension_id)
                operation = InstallOperation(extension_id, self._root / 'logs' / f'{extension_id}.log')
                self._operation = operation
                self._failures.pop(extension_id, None)
                self._details.pop(extension_id, None)
                self._task = asyncio.create_task(self._install_and_refresh(operation, refresh),
                                                 name=f'install-extension-{extension_id}')
                self._task.add_done_callback(self._report_task_failure)
            return self.status(extension_id)

    async def _install_and_refresh(self, operation: InstallOperation, refresh: Callable[[], object]) -> None:
        extension_id = operation.extension_id
        manifest = self._manifest(extension_id)
        previous = self._records.get(extension_id)
        try:
            plan = await asyncio.to_thread(self._payloads[extension_id].environments.ensure, manifest, operation)
            record = ExtensionRecord(version=manifest.version, installed=True, enabled=True,
                                     environment_id=plan.environment_id)
            await asyncio.to_thread(self._prepare_install, manifest, record, operation)
            operation.check_cancelled()
            self._commit_record(extension_id, record)
            await asyncio.to_thread(refresh)
        except ExtensionInstallCancelled:
            self._commit_record(extension_id, previous)
            with self._lock:
                self._details[extension_id] = 'Installation cancelled'
        except Exception as exc:
            logger.exception('Extension installation failed: %s', extension_id)
            with self._lock:
                self._failures[extension_id] = f'{type(exc).__name__}: {exc}'
            self._commit_record(extension_id, previous)
        finally:
            with self._lock:
                self._operation = None

    async def wait_installation(self, extension_id: str) -> None:
        task = self._task
        if task is not None:
            await task
        status = self.status(extension_id)
        if status.state not in {'installed', 'disabled'}:
            raise RuntimeError(status.detail or f'Installation did not complete: {extension_id}')

    async def wait_preparation(self, identifier: str) -> None:
        task = self._environment_refresh_task
        if task is not None:
            await task
        status = self.runtime_registry.status(identifier)
        if status.state != 'ready' or status.detail == 'Preparation cancelled':
            raise RuntimeError(status.detail or f'Environment preparation did not complete: {identifier}')

    @staticmethod
    def _report_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error('Extension installation task failed unexpectedly',
                         exc_info=(type(error), error, error.__traceback__))

    def _referenced_environments(self) -> set[str]:
        return {record.environment_id for record in self._records.values()
                if record.installed and record.environment_id is not None}

    async def cancel(self, extension_id: str) -> ExtensionStatus:
        self._manifest(extension_id)
        with self._lock:
            operation, task = self._operation, self._task
        if operation is None or operation.extension_id != extension_id or task is None:
            return self.status(extension_id)
        await asyncio.to_thread(operation.cancel)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=20)
        except TimeoutError:
            await asyncio.to_thread(operation.cancel, force=True)
            await asyncio.shield(task)
        return self.status(extension_id)

    def installation_running(self, extension_id: str) -> bool:
        with self._lock:
            return self._operation is not None and self._operation.extension_id == extension_id

    async def close(self) -> None:
        await self.runtime_registry.close()
        if self._environment_refresh_task is not None:
            await self._environment_refresh_task
        with self._lock:
            operation = self._operation
        if operation is not None:
            await self.cancel(operation.extension_id)

    async def set_enabled(self, extension_id: str, enabled: bool, refresh: Callable[[], object],
                          is_running: Callable[[str], bool]) -> ExtensionStatus:
        async with self._actions:
            manifest = self._manifest(extension_id)
            with self._lock:
                self._require_idle()
                previous = self._records.get(extension_id)
                if previous is None or not previous.installed:
                    raise InvalidRequestError('Install the extension before changing its state')
                if not enabled and self._tool_running(extension_id):
                    raise ConflictError('An extension tool is running; cancel it before disabling')
                if not enabled and any(is_running(name) for name in manifest.service_classes):
                    raise ConflictError('Stop running services before disabling the extension')
                if previous.enabled == enabled:
                    return self.status(extension_id)
            await self._refresh_record(extension_id, copy_model(previous, update={'enabled': enabled}), refresh)
            return self.status(extension_id)

    async def uninstall(self, extension_id: str, refresh: Callable[[], object],
                        is_running: Callable[[str], bool]) -> ExtensionStatus:
        async with self._actions:
            manifest = self._manifest(extension_id)
            with self._lock:
                self._require_idle()
                previous = self._records.get(extension_id)
                if previous is None or not previous.installed:
                    return self.status(extension_id)
                if self._tool_running(extension_id):
                    raise ConflictError('An extension tool is running; cancel it before uninstalling')
                if any(is_running(name) for name in manifest.service_classes):
                    raise ConflictError('Stop running services before uninstalling the extension')
            await self._refresh_record(extension_id, ExtensionRecord(version=manifest.version, installed=False, enabled=False),
                                       refresh)
            with self._lock:
                referenced = self._referenced_environments()
            await asyncio.to_thread(self._payloads[extension_id].environments.remove_unused,
                                    previous.environment_id, referenced)
            await asyncio.to_thread(shutil.rmtree, self._registration(extension_id).parent)
            return self.status(extension_id)

    def _write_registration(self, manifest: ExtensionManifest, record: ExtensionRecord) -> None:
        payload = self._payloads[manifest.extension_id]
        destination = self._registration(manifest.extension_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        services: list[IndexedService] = []
        model_root = str(index_paths(payload.index_path, payload.index).model_root)
        if manifest.model_directories:
            model_root = str(self._model_root())
        plan = payload.environments.plan(manifest)
        if manifest.runtime.kind in {'pixi', 'shared'}:
            if record.environment_id != plan.environment_id or not payload.environments.ready(record.environment_id):
                raise ValueError(f'Environment for {manifest.extension_id} changed or is missing; reinstall the extension')
        for name in manifest.service_classes:
            item = self._services[name]
            entry = self._entry(manifest, name)
            environment = manifest.runtime.environment
            if manifest.runtime.kind == 'pixi' and entry.launch.command in {'python', 'python.exe'}:
                assert environment is not None
                command, args = payload.environments.python_launch(plan, environment)
                entry = copy_model(entry, update={'launch': copy_model(entry.launch, update={
                    'command': command, 'args': [*args, *(entry.launch.args or [])],
                    'workdir': str(payload.environments.workspace(plan)),
                })})
            elif manifest.runtime.kind in {'pixi', 'workspace'}:
                assert environment is not None
                command, args = payload.environments.launch(plan, environment)
                entry = copy_model(entry, update={'launch': copy_model(entry.launch, update={
                    'command': command, 'args': [*args, (entry.launch.args or [])[-1]],
                    'workdir': str(payload.environments.workspace(plan)),
                })})
            elif manifest.runtime.kind == 'shared':
                assert environment is not None
                python_root = destination.parent / 'python'
                if not python_root.is_dir():
                    raise ValueError(f'Python package for {manifest.extension_id} is missing; reinstall the extension')
                command, args = payload.environments.python_launch(plan, environment)
                entry = copy_model(entry, update={'launch': copy_model(entry.launch, update={
                    'command': command, 'args': [*args, str(Path(__file__).with_name('_shared_entrypoint.py')),
                                               str(python_root), *(entry.launch.args or [])[1:]],
                    'workdir': str(destination.parent),
                })})
            env = dict(entry.launch.env or {})
            env['F8_MODEL_ROOT'] = model_root
            entry = copy_model(entry, update={'launch': copy_model(entry.launch, update={'env': env})})
            entry_path = destination.parent / f'{name}.yml'
            entry_path.write_text(yaml.safe_dump(msgspec.to_builtins(entry), sort_keys=False), encoding='utf-8')
            describe = index_paths(payload.index_path, payload.index, item).package_path(item.describe, relative_to=payload.index_path.parent)
            if not describe.is_relative_to(payload.root):
                raise ValueError(f'Description for {name} is outside the extension payload')
            services.append(IndexedService(serviceClass=name, manifests={'any': entry_path.name}, describe=str(describe)))
        index = ServiceIndex(schemaVersion='f8serviceIndex/1', services=tuple(services), modelRoot=model_root)
        temporary = destination.with_suffix('.tmp')
        temporary.write_bytes(msgspec.json.encode(index))
        temporary.replace(destination)

    def _model_root(self) -> Path:
        if os.environ.get('F8_MODEL_ROOT') or os.environ.get('F8_RESOURCE_ROOT'):
            return configured_model_root().resolve()
        return (self._data_dir / 'models').resolve()

    def _copy_model_metadata(self, manifest: ExtensionManifest) -> None:
        for directory in manifest.model_directories:
            source = self._payloads[manifest.extension_id].index_path.parent.parent / 'resources' / 'models' / directory
            destination = self._model_root() / directory
            destination.mkdir(parents=True, exist_ok=True)
            for metadata in sorted(source.glob('*.yaml')):
                if not (destination / metadata.name).exists():
                    shutil.copy2(metadata, destination / metadata.name)

    def _prepare_install(self, manifest: ExtensionManifest, record: ExtensionRecord,
                         operation: InstallOperation) -> None:
        operation.check_cancelled()
        if manifest.runtime.kind == 'shared':
            self.runtime_registry.validate_compatibility(manifest)
            payload = self._payloads[manifest.extension_id]
            environment = manifest.runtime.environment
            assert environment is not None
            plan = payload.environments.plan(manifest)
            command, args = payload.environments.python_launch(plan, environment)
            operation.report(f'Checking dependencies against selected runtime {environment}')
            # Probe tooling is copied without dist-info so even a bare interpreter
            # can report wheel tags, while dependency discovery remains target-only.
            with tempfile.TemporaryDirectory(prefix='f8-runtime-probe-') as temporary:
                probe_root = Path(temporary)
                shutil.copytree(Path(packaging.__file__).parent, probe_root / 'packaging',
                                ignore=shutil.ignore_patterns('__pycache__'))
                output = operation.run([command, *args, str(Path(__file__).with_name('_runtime_probe.py')), str(probe_root)],
                                       cwd=payload.environments.workspace(plan), timeout=120)
            validate_shared_package(manifest, payload.root / 'python',
                                    msgspec.json.decode(output.encode(), type=RuntimeProbe))
            operation.check_cancelled()
            destination = self._registration(manifest.extension_id).parent / 'python'
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(payload.root / 'python', destination)
        self._copy_model_metadata(manifest)
        self._write_registration(manifest, record)
        catalog = ServiceCatalog()
        load_index_into_catalog(path=self._registration(manifest.extension_id), catalog=catalog)
        for service_class in manifest.service_classes:
            operation.check_cancelled()
            entry = catalog.service_entry(service_class)
            if entry is None:
                raise ValueError(f'Missing launcher for {service_class}')
            operation.report(f'Checking {service_class}')
            output = operation.run([entry.launch.command, *(entry.launch.args or []),
                                    *(entry.describeArgs or ['--describe'])],
                                   cwd=Path(entry.launch.workdir or self._source_root), timeout=120,
                                   env={**os.environ, **(entry.launch.env or {})})
            payload = msgspec.json.decode(output.encode(), type=dict[str, object])
            validate_describe_monitor_contract(payload)
            describe = validate_as(F8ServiceDescribe, payload)
            if describe.service.serviceClass != service_class:
                raise ValueError(f'Wrong service contract from {service_class}')
