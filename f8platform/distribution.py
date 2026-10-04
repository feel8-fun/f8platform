"""Import a pinned distribution into writable component storage."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import shutil

import msgspec

from f8pysdk.component_spec import ComponentCatalog
from .components import ComponentManager
from .errors import ConflictError, InvalidRequestError


def distribution_root(data_dir: Path, source: Path) -> Path:
    destination = data_dir / 'distribution'
    if destination.exists() and destination.resolve() == source.resolve():
        return destination
    config = source / 'config'
    if not (config / 'service-index.json').is_file():
        raise InvalidRequestError('Distribution requires a service index')
    # Preserve immutable artifacts and config under a stable data root. Components
    # refer to this index, whose package roots remain valid after bootstrap relocation.
    destination.mkdir(parents=True, exist_ok=True)
    for name in ('config', 'extension-packages', 'runtime-providers', 'environment-definitions', 'wheels'):
        if (source / name).is_dir():
            shutil.copytree(source / name, destination / name, dirs_exist_ok=True)
    return destination


async def install_distribution(manager: ComponentManager, source: Path) -> tuple[str, ...]:
    catalog = msgspec.json.decode((source / 'config/component-packages.json').read_bytes(), type=ComponentCatalog)
    if manager.running:
        raise ConflictError('Stop component processes before installing a distribution')
    selected = dict(manager.state.selected)
    for item in catalog.components:
        prefix = '${F8_PACKAGE_ROOT}/'
        if not item.path.startswith(prefix):
            raise InvalidRequestError('Bundled component path requires a package root')
        payload = (source / item.path.removeprefix(prefix)).resolve()
        if not payload.is_relative_to(source.resolve()) or payload.name != item.sha256:
            raise InvalidRequestError('Bundled component requires a contained content-addressed path')
        archive = source / 'component-archives' / f'{item.sha256}.zip'
        if not archive.is_file():
            raise InvalidRequestError('Distribution must retain original component archives')
        with archive.open('rb') as handle:
            if hashlib.file_digest(handle, 'sha256').hexdigest() != item.sha256:
                raise InvalidRequestError('Bundled component checksum mismatch')
        record = await manager.import_archive(str(archive.resolve()), item.sha256)
        await manager.prepare(record.component_id, record.sha256)
        selected[record.component_id] = record.sha256
    manager.validate_selection(selected)
    if set(catalog.startup) - selected.keys():
        raise InvalidRequestError('Distribution startup component is missing')
    await asyncio.to_thread(distribution_root, manager.data_dir, source)
    await manager.select_all(selected)
    return catalog.startup
