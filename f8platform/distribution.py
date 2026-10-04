"""Install a pinned distribution of ordinary extension artifacts."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shutil

import msgspec

from f8pysdk.application_package import read_application_extension
from f8pysdk.release_spec import BundledExtensionCatalog, PlatformReleaseLock
from .applications import ApplicationManager
from .errors import ConflictError, InvalidRequestError


def distribution_root(data_dir: Path, source: Path) -> Path:
    destination = data_dir / 'distribution'
    if destination.resolve() == source.resolve():
        return destination
    if not (source / 'config/service-index.json').is_file():
        raise InvalidRequestError('Distribution requires a service index')
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('config', 'extension-packages', 'extension-archives', 'runtime-providers', 'environment-definitions', 'wheels'):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name, dirs_exist_ok=True)
    return destination


async def install_distribution(manager: ApplicationManager, source: Path) -> tuple[str, ...]:
    catalog = msgspec.json.decode((source / 'config/extension-packages.json').read_bytes(), type=BundledExtensionCatalog)
    lock = msgspec.json.decode((source / 'config/release-lock.json').read_bytes(), type=PlatformReleaseLock)
    if manager.running:
        raise ConflictError('Stop application processes before installing a distribution')
    selected = dict(manager.state.selected)
    for item in catalog.packages:
        prefix = '${F8_PACKAGE_ROOT}/'
        if not item.path.startswith(prefix):
            raise InvalidRequestError('Bundled extension requires a package root')
        payload = (source / item.path.removeprefix(prefix)).resolve()
        if not payload.is_relative_to(source.resolve()) or payload.name != item.sha256:
            raise InvalidRequestError('Bundled extension requires a contained content-addressed path')
        extension_path = payload / 'config/extensions.json'
        from f8pysdk.extension_spec import ExtensionCatalog
        extensions = msgspec.json.decode(extension_path.read_bytes(), type=ExtensionCatalog)
        if len(extensions.extensions) != 1:
            raise InvalidRequestError('Bundled artifact must own one extension')
        if extensions.extensions[0].application is None:
            continue
        read_application_extension(payload)
        archive = source / 'extension-archives' / f'{item.sha256}.zip'
        if not archive.is_file():
            raise InvalidRequestError('Distribution must retain original extension archives')
        with archive.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != item.sha256:
                raise InvalidRequestError('Bundled extension checksum mismatch')
        record = await manager.import_archive(str(archive.resolve()), item.sha256)
        selected.setdefault(record.extension_id, record.sha256)
        await manager.prepare(record.extension_id, selected[record.extension_id])
    manager.validate_selection(selected)
    if set(lock.startup) - selected.keys():
        raise InvalidRequestError('Distribution startup application is missing')
    await asyncio.to_thread(distribution_root, manager.data_dir, source)
    await manager.select_all(selected)
    return lock.startup
