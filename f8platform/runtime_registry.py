"""Inventory and maintenance of extension-declared Pixi environments."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import contextmanager
from collections.abc import Generator
import logging
import re
from pathlib import Path
import shutil
from threading import Lock, RLock
import time
from typing import Literal

import msgspec
from packaging.specifiers import SpecifierSet
from f8pysdk.codec import copy_model
from f8pysdk.extension_status import (
    EnvironmentDetail,
    EnvironmentStatus,
    RuntimeStorageStatus,
    UnusedEnvironment,
)
from f8pysdk.release_spec import RuntimeDefinition
from .environment_definitions import selected_manifest, toml_text
from .environments import EnvironmentManager, SharedRuntimeTarget
from .errors import ConflictError, InvalidRequestError, NotFoundError
from .extension_models import ExtensionManifest, ExtensionRuntime
from .extension_operation import ExtensionInstallCancelled, InstallOperation
from .runtime_inspection import (
    directory_usage as directory_usage,
    environment_packages,
    storage_usage,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeSource:
    name: str
    source: Literal["official", "package"]
    target: SharedRuntimeTarget
    manifest: ExtensionManifest | None = None
    release: RuntimeDefinition | None = None


@dataclass(frozen=True)
class RuntimeSourcesSnapshot:
    sources: dict[str, RuntimeSource]
    aliases: dict[str, str]
    shared_targets: dict[str, SharedRuntimeTarget]


@dataclass(frozen=True)
class StorageSnapshot:
    roots: tuple[Path, ...]
    cache: Path
    referenced: frozenset[str]
    generation: int
    measured_at: float
    status: RuntimeStorageStatus


class RuntimeRegistry:
    def __init__(self, data_dir: Path, official: EnvironmentManager) -> None:
        self.data_dir = data_dir
        self.official = official
        self.storage = official.root.parent
        self.definitions = data_dir / "runtime-definitions"
        self._source_history_file = self.definitions / "official-sources.json"
        self._source_history = (
            msgspec.json.decode(self._source_history_file.read_bytes(), type=dict[str, str])
            if self._source_history_file.is_file()
            else {}
        )
        self._lock = RLock()
        self._usage_lock = Lock()
        self._usage_snapshot: StorageSnapshot | None = None
        self._usage_generation = 0
        self.sources: dict[str, RuntimeSource] = {}
        self._aliases: dict[str, str] = {}
        self._operation: InstallOperation | None = None
        self._task: asyncio.Task[None] | None = None
        self._maintenance_active = False
        self._progress: dict[str, str] = {}
        self._failures: dict[str, str] = {}
        if official.has_runtime_catalog:
            for name in official.preset_names():
                self.add_preset(name)

    @property
    def busy(self) -> bool:
        return self._operation is not None or self._maintenance_active

    @contextmanager
    def maintenance(self) -> Generator[None]:
        with self._lock:
            if self.busy:
                raise ConflictError("Wait for runtime maintenance to finish")
            self._maintenance_active = True
        try:
            yield
        finally:
            with self._lock:
                self._maintenance_active = False
            self.invalidate_usage()

    def invalidate_usage(self) -> None:
        with self._lock:
            self._usage_generation += 1

    @property
    def preparation_task(self) -> asyncio.Task[None] | None:
        return self._task

    def snapshot_sources(self) -> RuntimeSourcesSnapshot:
        return RuntimeSourcesSnapshot(
            sources=dict(self.sources), aliases=dict(self._aliases), shared_targets=dict(self.official.shared_targets)
        )

    def restore_sources(self, snapshot: RuntimeSourcesSnapshot) -> None:
        self.sources = snapshot.sources
        self._aliases = snapshot.aliases
        self.official.shared_targets = snapshot.shared_targets

    def add_source(self, manager: EnvironmentManager, manifest: ExtensionManifest, *, official: bool) -> None:
        environment = manifest.runtime.environment
        if manifest.runtime.kind == "shared":
            if environment is not None:
                self.add_preset(environment)
            return
        if environment is None or manifest.runtime.kind == "native":
            return
        owners = (
            tuple(manager.for_environment(name) for name in manager.preset_names())
            if not official and manager.has_runtime_catalog
            else (manager.for_environment(environment),)
        )
        seen: set[tuple[Path, str]] = set()
        for owner in owners:
            names = (environment,) if official and not manager.has_runtime_catalog else owner.preset_names()
            for name in names:
                key = (owner.source_root, name)
                if key in seen:
                    continue
                seen.add(key)
                selected = copy_model(
                    manifest,
                    update={
                        "runtime": copy_model(manifest.runtime, update={"environment": name}),
                    },
                )
                plan = owner.plan(selected)
                assert plan.environment_id is not None
                release = manager.runtime_releases.get(name)
                source = RuntimeSource(
                    name=name,
                    source="official" if official else "package",
                    target=SharedRuntimeTarget(owner, plan, name),
                    manifest=selected,
                    release=release,
                )
                self.sources[plan.environment_id] = source
                self.official.shared_targets[plan.environment_id] = source.target
                self._register_source_alias(source)

    def add_preset(self, environment: str) -> None:
        if environment not in self.official.preset_names():
            return
        manager = self.official.for_environment(environment)
        plan = manager.preset_plan(environment)
        assert plan.environment_id is not None
        manifest = ExtensionManifest(
            extension_id="runtime-provider",
            name=environment,
            version="1",
            description="",
            runtime=ExtensionRuntime(kind=plan.runtime_kind, environment=environment),
        )
        target = SharedRuntimeTarget(manager, plan, environment)
        self.sources[plan.environment_id] = RuntimeSource(
            name=environment,
            source="official",
            target=target,
            manifest=manifest,
            release=self.official.runtime_releases.get(environment),
        )
        self.official.shared_targets[plan.environment_id] = target
        self._register_source_alias(self.sources[plan.environment_id])

    def _register_source_alias(self, source: RuntimeSource) -> None:
        previous = source.target.manager.legacy_environment_id(source.target.environment)
        identifier = source.target.plan.environment_id
        if identifier is None:
            return
        if previous is not None:
            self._aliases[previous] = identifier
            self.official.shared_targets[previous] = source.target
        if (
            source.source == "official"
            and self.official.has_runtime_catalog
            and self.official.runtime_releases[source.name].version is None
            and source.target.manager is self.official.for_environment(source.name)
        ):
            for old_id, name in self._source_history.items():
                if name == source.name and old_id != identifier:
                    self._aliases[old_id] = identifier
                    self.official.shared_targets[old_id] = source.target
            if identifier not in self._source_history:
                self._source_history[identifier] = source.name
                self.definitions.mkdir(parents=True, exist_ok=True)
                temporary = self._source_history_file.with_suffix(".tmp")
                temporary.write_bytes(msgspec.json.encode(self._source_history))
                temporary.replace(self._source_history_file)

    def validate_compatibility(self, manifest: ExtensionManifest, identifier: str | None = None) -> None:
        runtime = manifest.runtime
        if runtime.provider_id is None and runtime.provider_version is None and runtime.abi is None:
            return
        definition = self.release_definition(identifier or runtime.environment or "")
        if definition is None:
            raise InvalidRequestError("Selected runtime has no publisher identity; choose a versioned runtime")
        if runtime.provider_id is not None and definition.provider_id != runtime.provider_id:
            raise InvalidRequestError(f"Extension requires runtime provider {runtime.provider_id}")
        if runtime.provider_version is not None and (
            definition.version is None
            or not SpecifierSet(runtime.provider_version).contains(definition.version, prereleases=True)
        ):
            raise InvalidRequestError(
                f"Runtime version {definition.version} does not satisfy {runtime.provider_version}"
            )
        if runtime.abi is not None and runtime.abi != definition.abi:
            raise InvalidRequestError(f"Extension requires runtime ABI {runtime.abi}; selected ABI is {definition.abi}")

    def canonical_id(self, identifier: str) -> str:
        return self._aliases.get(identifier, identifier)

    def source(self, identifier: str) -> RuntimeSource:
        identifier = self._aliases.get(identifier, identifier)
        source = self.sources.get(identifier)
        if source is None:
            raise NotFoundError(f"Environment is not prepared or does not exist: {identifier}")
        return source

    def source_snapshot(self) -> dict[str, RuntimeSource]:
        with self._lock:
            return dict(self.sources)

    def release_definition(self, identifier: str) -> RuntimeDefinition | None:
        source = self.sources.get(self.canonical_id(identifier))
        return source.release if source is not None else self.official.runtime_releases.get(identifier)

    def status(self, identifier: str) -> EnvironmentStatus:
        identifier = self.canonical_id(identifier)
        source = self.source(identifier)
        ready = source.target.manager.ready(source.target.plan.environment_id)
        prefix_exists = source.target.manager.workspace_python_exists(
            source.target.environment
        ) or source.target.manager.development_python_exists(source.target.environment)
        state = (
            "ready"
            if ready
            else "changed"
            if source.target.plan.runtime_kind == "workspace" and prefix_exists
            else "missing"
        )
        return EnvironmentStatus(
            environment_id=identifier,
            runtime_kind=source.target.plan.runtime_kind,
            extension_ids=(),
            ready=ready,
            name=source.name,
            source=source.source,
            revision=identifier.rsplit("-", 1)[-1][:12],
            state="preparing"
            if self._operation is not None and self._operation.extension_id == identifier
            else "failed"
            if identifier in self._failures
            else state,
            detail=self._operation.detail
            if self._operation is not None and self._operation.extension_id == identifier
            else self._progress.get(identifier, ""),
        )

    async def prepare(self, identifier: str) -> EnvironmentStatus:
        with self._lock:
            if self.busy:
                raise ConflictError("Runtime maintenance is already running")
            self.source(identifier)
            self._failures.pop(identifier, None)
            operation = InstallOperation(identifier, self.definitions / "logs" / f"{identifier}.log")
            self._operation = operation
            self._task = asyncio.create_task(self._prepare(operation), name=f"prepare-runtime-{identifier}")
        return self.status(identifier)

    async def _prepare(self, operation: InstallOperation) -> None:
        identifier = operation.extension_id
        try:
            source = self.source(identifier)
            if source.manifest is None:
                raise InvalidRequestError("Environment source has no install declaration")
            if source.manifest.runtime.kind == "workspace":
                pixi = await asyncio.to_thread(source.target.manager.pixi_executable, operation)
                await asyncio.to_thread(
                    operation.run,
                    [
                        str(pixi),
                        "install",
                        "--locked",
                        "-e",
                        source.target.environment,
                        "--manifest-path",
                        str(source.target.manager.source_root / "pixi.toml"),
                    ],
                    cwd=source.target.manager.source_root,
                    env=source.target.manager.install_environment(),
                )
            else:
                await asyncio.to_thread(source.target.manager.ensure, source.manifest, operation)
            current_plan = source.target.manager.plan(source.manifest)
            assert current_plan.environment_id is not None
            target = SharedRuntimeTarget(source.target.manager, current_plan, source.target.environment)
            with self._lock:
                self.sources.pop(identifier)
                self.sources[current_plan.environment_id] = RuntimeSource(
                    name=source.name,
                    source=source.source,
                    target=target,
                    manifest=source.manifest,
                    release=source.release,
                )
                self.official.shared_targets[identifier] = target
                self.official.shared_targets[current_plan.environment_id] = target
                self._aliases[identifier] = current_plan.environment_id
                self._register_source_alias(self.sources[current_plan.environment_id])
                self._progress[current_plan.environment_id] = "Environment prepared successfully"
        except ExtensionInstallCancelled:
            self._progress[identifier] = "Preparation cancelled"
        except Exception as exc:
            logger.exception("Cannot prepare runtime %s", identifier)
            message = f"{type(exc).__name__}: {exc}"
            self._progress[identifier] = message
            self._failures[identifier] = message
        finally:
            with self._lock:
                self._operation = None
            self.invalidate_usage()

    def remove(self, identifier: str, referenced: set[str]) -> None:
        with self._lock:
            if self.busy:
                raise ConflictError("Wait for runtime maintenance to finish")
            identifier = self.canonical_id(identifier)
            if identifier in referenced:
                raise ConflictError("Uninstall extensions before releasing their environment")
            source = self.source(identifier)
            if source.target.plan.runtime_kind != "pixi":
                raise InvalidRequestError("Development and bundled environments cannot be released here")
            source.target.manager.remove_unused(identifier, referenced)
            self._progress[identifier] = "Environment files released; Pixi will recreate them when needed"
            self.invalidate_usage()

    def prefix(self, source: RuntimeSource) -> Path:
        workspace = source.target.manager.workspace(source.target.plan)
        return (
            workspace / "env"
            if source.target.plan.runtime_kind == "bundled"
            else workspace / ".pixi/envs" / source.target.environment
        )

    def detail(self, identifier: str) -> EnvironmentDetail:
        identifier = self.canonical_id(identifier)
        source = self.source(identifier)
        root = source.target.manager.source_root
        prefix = self.prefix(source)
        inventory, packages = environment_packages(root, prefix, source.target.environment)
        release = source.release
        return EnvironmentDetail(
            environment_id=identifier,
            name=source.name,
            revision=identifier.rsplit("-", 1)[-1][:12],
            manifest=toml_text(selected_manifest(root, source.target.environment)),
            storage_path=str(prefix),
            cache_path=str(self.storage / "package-cache"),
            definition_path=str(root / "pixi.toml"),
            source_environment=source.target.environment,
            provider_id=release.provider_id if release else None,
            provider_version=release.version if release else None,
            abi=release.abi if release else None,
            usage=directory_usage(prefix),
            packages=packages,
            package_inventory=inventory,
        )

    def inspection_roots(self) -> tuple[Path, ...]:
        roots = {self.storage / "runtimes"}
        roots.update(self.prefix(source) for source in self.source_snapshot().values())
        return tuple(
            sorted(path for path in roots if not any(path != other and path.is_relative_to(other) for other in roots))
        )

    def unused_environment_paths(self, referenced: set[str]) -> tuple[Path, ...]:
        root = self.storage / "runtimes"
        if root.is_symlink() or not root.is_dir():
            return ()
        return tuple(
            path
            for path in sorted(root.iterdir())
            if path.is_dir()
            and not path.is_symlink()
            and re.fullmatch(r'(?:pixi-[0-9a-f]{16}-[A-Za-z0-9_.-]+-[0-9a-f]{64}|user-[0-9a-f]{64})(?:\.tmp)?', path.name)
            and path.name not in referenced
            and (path / "pixi.toml").is_file()
        )

    def unused_environments(self, referenced: set[str]) -> tuple[UnusedEnvironment, ...]:
        return tuple(UnusedEnvironment(environment_id=path.name, path=str(path), usage=directory_usage(path))
                     for path in self.unused_environment_paths(referenced))

    def clean_unused_environments(self, referenced: set[str]) -> None:
        if not self._maintenance_active:
            raise RuntimeError("Runtime cleanup requires the maintenance guard")
        for path in self.unused_environment_paths(referenced):
            root = self.storage / "runtimes"
            if root.is_symlink() or path.is_symlink() or path.resolve().parent != root.resolve():
                raise ConflictError("Managed environment directory moved during cleanup")
            shutil.rmtree(path)
            self._progress[path.name] = "Unused environment files released"
        self.invalidate_usage()

    def storage_status(self, referenced: set[str] | None = None, *, refresh: bool = False) -> RuntimeStorageStatus:
        requested_at = time.monotonic()
        cache = self.storage / "package-cache"
        roots = self.inspection_roots()
        references = set(self.source_snapshot()) if referenced is None else referenced
        managed = self.storage / "runtimes"
        occupied = managed.is_dir() and any(managed.iterdir())
        with self._usage_lock:
            with self._lock:
                generation = self._usage_generation
            previous = self._usage_snapshot
            if (previous is not None and previous.roots == roots and previous.cache == cache
                    and previous.referenced == frozenset(references) and previous.generation == generation
                    and time.monotonic() - previous.measured_at < 30
                    and (not refresh or previous.measured_at >= requested_at)):
                return copy_model(previous.status, update={'can_change': not self.busy and not occupied})
            unused = self.unused_environment_paths(references)
            usage = storage_usage(roots, cache, unused)
            status = RuntimeStorageStatus(path=str(self.storage), cache_path=str(cache),
                can_change=not self.busy and not occupied, environment_usage=usage.environments,
                cache_usage=usage.cache, total_usage=usage.total,
                unused_environments=tuple(UnusedEnvironment(environment_id=path.name, path=str(path), usage=usage.unused[path]) for path in unused),
                usage_updated_at=time.time())
            self._usage_snapshot = StorageSnapshot(roots, cache, frozenset(references), generation, time.monotonic(), status)
            return status

    async def cancel(self, identifier: str) -> EnvironmentStatus:
        operation, task = self._operation, self._task
        if operation is not None and operation.extension_id == identifier and task is not None:
            await asyncio.to_thread(operation.cancel)
            await task
        return self.status(identifier)

    def preparation_running(self, identifier: str) -> bool:
        with self._lock:
            return self._operation is not None and self._operation.extension_id == identifier

    async def close(self) -> None:
        if self._operation is not None:
            await self.cancel(self._operation.extension_id)

    def set_storage(self, path: str) -> RuntimeStorageStatus:
        if not self.storage_status().can_change:
            raise ConflictError("Remove prepared managed environments before changing runtime storage")
        destination = Path(path).expanduser()
        if not destination.is_absolute():
            raise InvalidRequestError("Runtime storage must be an absolute directory path")
        destination.mkdir(parents=True, exist_ok=True)
        destination = destination.resolve()
        temporary = self.data_dir / "runtime-storage.tmp"
        temporary.write_bytes(msgspec.json.encode({"path": str(destination)}))
        temporary.replace(self.data_dir / "runtime-storage.json")
        self.storage = destination
        self.invalidate_usage()
        self.official.set_runtime_storage(destination)
        for source in self.sources.values():
            source.target.manager.root = destination / "runtimes"
        return self.storage_status()
