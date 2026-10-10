"""Extension catalog contracts and preflight validation."""
from __future__ import annotations

import msgspec
from dataclasses import dataclass
from pathlib import Path
import re
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from f8pysdk.extension_capabilities import validate_capabilities
from f8pysdk.service_paths import ServicePaths
from f8pysdk.service_runtime_tools.inventory.index import IndexedService, ServiceIndex
from .runtime_sources import read_runtime_catalog
from f8pysdk.release_spec import PublishedArtifact
from f8pysdk.service_runtime_tools.inventory.index import read_service_index, index_paths
import sys
import tomllib

from .environments import EnvironmentManager
from .extension_models import ExtensionCatalog

@dataclass(frozen=True)
class ExtensionPayload:
    root: Path
    index_path: Path
    index: ServiceIndex
    environments: EnvironmentManager
    source_checkout: bool
    release_sha256: str | None


def validate_extension_catalog( catalog: ExtensionCatalog, services: dict[str, IndexedService], *, root: Path) -> dict[str, str]:
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


@dataclass(frozen=True)
class CatalogCandidate:
    root: Path
    index_path: Path
    catalog: ExtensionCatalog
    index: ServiceIndex
    services: dict[str, IndexedService]
    owners: dict[str, str]
    development_sources: dict[str, str]
    published_artifact: bool


def read_extension_catalog(root: Path, *, preinstalled: bool, index_path: Path | None = None) -> CatalogCandidate:
    root = root.resolve()
    index_path = index_path or root / 'config/service-index.json'
    catalog = msgspec.json.decode(index_path.with_name('extensions.json').read_bytes(), type=ExtensionCatalog)
    index = (read_service_index(index_path) if index_path.is_file() else
             ServiceIndex(schemaVersion='f8serviceIndex/1', services=(), modelRoot='${F8_MODEL_ROOT}'))
    services = {item.serviceClass: item for item in index.services}
    owners = validate_extension_catalog(catalog, services, root=root)
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
    return CatalogCandidate(root=root, index_path=index_path, catalog=catalog, index=index,
                            services=services, owners=owners, development_sources=development_sources,
                            published_artifact=descriptor_path.is_file())
