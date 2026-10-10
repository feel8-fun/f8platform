"""Installed service registration and model metadata ownership."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import msgspec
import yaml
from f8pysdk.codec import copy_model
from f8pysdk.resource_paths import model_root as configured_model_root
from f8pysdk.service_runtime_tools.inventory.index import IndexedService, ServiceIndex, index_paths, indexed_entry
from f8pysdk.specs import F8ServiceEntry
from .errors import InvalidRequestError
from .extension_models import ExtensionManifest, ExtensionRecord
from .extension_catalog import ExtensionPayload

def validated_service_entry(manifest: ExtensionManifest, payload: ExtensionPayload, item: IndexedService) -> F8ServiceEntry:
    service_class = item.serviceClass
    entry = indexed_entry(payload.index_path, payload.index, item)
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


class RegistrationStore:
    def __init__(self, *, root: Path, data_dir: Path) -> None:
        self._root = root
        self._data_dir = data_dir

    def path(self, extension_id: str) -> Path:
        return self._root / 'registrations' / extension_id / 'service-index.json'

    def write(self, manifest: ExtensionManifest, record: ExtensionRecord,
              payload: ExtensionPayload, services_by_class: dict[str, IndexedService]) -> None:
        destination = self.path(manifest.extension_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        services: list[IndexedService] = []
        model_root = str(index_paths(payload.index_path, payload.index).model_root)
        if manifest.model_directories:
            model_root = str(self.model_root())
        plan = payload.environments.plan(manifest)
        if manifest.runtime.kind in {'pixi', 'shared'}:
            if record.environment_id != plan.environment_id or not payload.environments.ready(record.environment_id):
                raise ValueError(f'Environment for {manifest.extension_id} changed or is missing; reinstall the extension')
        for name in manifest.service_classes:
            item = services_by_class[name]
            entry = validated_service_entry(manifest, payload, item)
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


    def model_root(self) -> Path:
        if os.environ.get('F8_MODEL_ROOT') or os.environ.get('F8_RESOURCE_ROOT'):
            return configured_model_root().resolve()
        return (self._data_dir / 'models').resolve()


    def copy_model_metadata(self, manifest: ExtensionManifest, payload: ExtensionPayload) -> None:
        for directory in manifest.model_directories:
            source = payload.index_path.parent.parent / 'resources' / 'models' / directory
            destination = self.model_root() / directory
            destination.mkdir(parents=True, exist_ok=True)
            for metadata in sorted(source.glob('*.yaml')):
                if not (destination / metadata.name).exists():
                    shutil.copy2(metadata, destination / metadata.name)
