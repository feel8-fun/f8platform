"""Assemble a release from immutable publishers; no application source builds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import urllib.parse
import urllib.request

import msgspec

from f8pysdk.application_package import validate_application
from f8pysdk.extension_packaging import validate_package
from f8pysdk.extension_spec import ExtensionCatalog
from f8pysdk.release_spec import (
    BundledExtensionCatalog, BundledExtensionPackage, PlatformReleaseLock,
    PublishedArtifact, ReleaseArtifact, RuntimeCatalog, RuntimeDefinition,
)
from f8pysdk.runtime_package import validate_runtime_package
from .applications import ApplicationManager
from .environment_definitions import materialize_locked_environment
from .extension_artifacts import MAX_ARCHIVE_BYTES, extract_archive
from .runtime_sources import read_runtime_sources

PLATFORMS = {'linux-x86_64', 'windows-x86_64'}


def fetch_artifact(artifact: ReleaseArtifact, root: Path, cache: Path) -> Path:
    if not re.fullmatch(r'[0-9a-f]{64}', artifact.sha256):
        raise ValueError(f'Invalid SHA-256 for {artifact.artifact_id}')
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f'{artifact.sha256}.zip'
    if not archive.is_file():
        temporary = archive.with_suffix('.download')
        parsed = urllib.parse.urlsplit(artifact.location)
        try:
            if parsed.scheme and not Path(artifact.location).is_absolute():
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Release artifacts require HTTPS without embedded credentials')
                with urllib.request.urlopen(artifact.location, timeout=30) as source, temporary.open('wb') as target:
                    if urllib.parse.urlsplit(str(source.geturl())).scheme != 'https':
                        raise ValueError('Artifact download redirected away from HTTPS')
                    total = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if total > MAX_ARCHIVE_BYTES:
                            raise ValueError('Release archive exceeds download limit')
                        target.write(chunk)
            else:
                source_path = (root / artifact.location).resolve()
                if source_path.stat().st_size > MAX_ARCHIVE_BYTES:
                    raise ValueError('Release archive exceeds download limit')
                shutil.copyfile(source_path, temporary)
            with temporary.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != artifact.sha256:
                    raise ValueError(f'Artifact checksum mismatch: {artifact.artifact_id}')
            temporary.replace(archive)
        finally:
            temporary.unlink(missing_ok=True)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != artifact.sha256:
            raise ValueError(f'Cached artifact checksum mismatch: {artifact.artifact_id}')
    return archive


def assemble_release(lock_path: Path, destination: Path, *, cache: Path, platform: str) -> tuple[str, ...]:
    lock = msgspec.json.decode(lock_path.read_bytes(), type=PlatformReleaseLock)
    if platform not in PLATFORMS or lock.platform != platform:
        raise ValueError('Release target platform mismatch')
    identities = [item.artifact_id for item in lock.artifacts]
    if len(identities) != len(set(identities)):
        raise ValueError('Release artifact IDs must be unique')
    if len(lock.startup) != len(set(lock.startup)):
        raise ValueError('Startup application identities must be unique')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Release destination must be empty')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='f8-release-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'release'
        staging.mkdir()
        definitions: list[RuntimeDefinition] = []
        packages: list[BundledExtensionPackage] = []
        validation = ApplicationManager(Path(temporary) / 'validation')
        selected: dict[str, str] = {}
        service_owners: set[str] = set()
        for artifact in lock.artifacts:
            archive = fetch_artifact(artifact, lock_path.parent, cache)
            relative = Path('runtime-providers' if artifact.kind == 'runtime' else 'extension-packages') / artifact.sha256
            payload = staging / relative
            payload.parent.mkdir(exist_ok=True)
            extract_archive(archive, payload, required=('config/artifact.json',))
            descriptor = msgspec.json.decode((payload / 'config/artifact.json').read_bytes(), type=PublishedArtifact)
            if (descriptor.artifact_id, descriptor.version, descriptor.kind) != (artifact.artifact_id, artifact.version, artifact.kind):
                raise ValueError('Artifact identity does not match release lock')
            if descriptor.platform not in {'any', platform}:
                raise ValueError('Artifact target platform mismatch')
            package_path = '${F8_PACKAGE_ROOT}/' + relative.as_posix()
            if artifact.kind == 'runtime':
                for definition in validate_runtime_package(payload).runtimes:
                    definitions.append(msgspec.structs.replace(definition,
                        manifest=package_path + '/' + definition.manifest.removeprefix('${F8_PACKAGE_ROOT}/')))
                continue
            # Publisher artifacts have the same catalog whether they offer nodes,
            # tools, applications, skills, or resources.
            catalog = msgspec.json.decode((payload / 'config/extensions.json').read_bytes(), type=ExtensionCatalog)
            validate_package(payload)
            extension = catalog.extensions[0]
            if ((extension.extension_id, extension.version) != (artifact.artifact_id, artifact.version)
                    or catalog.preinstalled or extension.runtime.kind not in {'native', 'pixi', 'shared'}):
                raise ValueError('Invalid independently installable extension')
            if service_owners.intersection(extension.service_classes):
                raise ValueError('Extensions have conflicting service ownership')
            service_owners.update(extension.service_classes)
            if extension.application is not None:
                validate_application(payload)
                record = validation.import_local_archive(str(archive.resolve()), artifact.sha256)
                selected[record.extension_id] = record.sha256
            archives = staging / 'extension-archives'
            archives.mkdir(exist_ok=True)
            shutil.copy2(archive, archives / f'{artifact.sha256}.zip')
            packages.append(BundledExtensionPackage(path=package_path, sha256=artifact.sha256))
        names = [item.runtime_id for item in definitions]
        if lock.base_runtime not in names or len(names) != len(set(names)):
            raise ValueError('Release requires a unique bootstrap runtime')
        validation.validate_selection(selected)
        if set(lock.startup) - selected.keys():
            raise ValueError('Startup references an unbundled application extension')
        config = staging / 'config'
        config.mkdir()
        (config / 'runtime-environments.json').write_bytes(msgspec.json.encode(RuntimeCatalog(
            schema_version='f8runtimeCatalog/1', runtimes=tuple(definitions))))
        (config / 'extension-packages.json').write_bytes(msgspec.json.encode(BundledExtensionCatalog(
            schema_version='f8extensionPackages/1', packages=tuple(packages))))
        (config / 'extensions.json').write_text(json.dumps({'schemaVersion': 'f8extensionCatalog/1', 'extensions': [], 'preinstalled': []}) + '\n')
        (config / 'service-index.json').write_text(json.dumps({'schemaVersion': 'f8serviceIndex/1', 'services': [], 'modelRoot': '${F8_MODEL_ROOT}'}) + '\n')
        sources = read_runtime_sources(staging)
        # Relocate only locked bootstrap wheel inputs, without solving or building.
        materialize_locked_environment(sources[lock.base_runtime][0], lock.base_runtime, staging, staging)
        (config / 'bootstrap.json').write_text(json.dumps({'environment': lock.base_runtime, 'module': 'f8platform'}) + '\n')
        (config / 'release-lock.json').write_bytes(msgspec.json.encode(lock))
        if destination.exists():
            destination.rmdir()
        staging.replace(destination)
    return tuple(names)
