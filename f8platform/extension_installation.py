"""Extension dependency and service validation at installation."""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import tempfile
import msgspec
import packaging
from f8pysdk.codec import validate_as
from f8pysdk.monitoring import validate_describe_monitor_contract
from f8pysdk.service_runtime_tools.inventory import ServiceCatalog
from f8pysdk.service_runtime_tools.inventory.index import IndexedService, load_index_into_catalog
from f8pysdk.specs import F8ServiceDescribe
from .runtime_registry import RuntimeRegistry
from .extension_models import ExtensionManifest, ExtensionRecord
from .extension_operation import InstallOperation
from .shared_dependencies import RuntimeProbe, validate_shared_package
from .extension_catalog import ExtensionPayload
from .extension_registration import RegistrationStore

def prepare_extension_install(manifest: ExtensionManifest, record: ExtensionRecord,
                              operation: InstallOperation, *, payload: ExtensionPayload,
                              services: dict[str, IndexedService], runtime_registry: RuntimeRegistry,
                              registrations: RegistrationStore, source_root: Path) -> None:
    operation.check_cancelled()
    if manifest.runtime.kind == 'shared':
        runtime_registry.validate_compatibility(manifest)
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
        destination = registrations.path(manifest.extension_id).parent / 'python'
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(payload.root / 'python', destination)
    registrations.copy_model_metadata(manifest, payload)
    registrations.write(manifest, record, payload, services)
    catalog = ServiceCatalog()
    load_index_into_catalog(path=registrations.path(manifest.extension_id), catalog=catalog)
    for service_class in manifest.service_classes:
        operation.check_cancelled()
        entry = catalog.service_entry(service_class)
        if entry is None:
            raise ValueError(f'Missing launcher for {service_class}')
        operation.report(f'Checking {service_class}')
        output = operation.run([entry.launch.command, *(entry.launch.args or []),
                                *(entry.describeArgs or ['--describe'])],
                               cwd=Path(entry.launch.workdir or source_root), timeout=120,
                               env={**os.environ, **(entry.launch.env or {})})
        describe_payload = msgspec.json.decode(output.encode(), type=dict[str, object])
        validate_describe_monitor_contract(describe_payload)
        describe = validate_as(F8ServiceDescribe, describe_payload)
        if describe.service.serviceClass != service_class:
            raise ValueError(f'Wrong service contract from {service_class}')
