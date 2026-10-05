"""The authoritative component inventory and management lifetime."""
from __future__ import annotations

from pathlib import Path
import asyncio
import msgspec
import logging

from f8pysdk.codec import copy_model
from f8pysdk.extension_status import ExtensionStatus, ExtensionCatalog, EnvironmentStatus, ExtensionDetail, ExtensionInstallPlan
from f8pysdk.platform_spec import PlatformInventory, DevelopmentCatalog, ApplicationStatus, ApplicationOperation
from f8pysdk.service_runtime_tools.inventory import ServiceCatalog
from f8pysdk.service_runtime_tools.inventory.index import load_index_into_catalog
from f8pysdk.specs import F8ServiceDescribe

from .applications import ApplicationManager
from .extensions import ExtensionManager
from .services import PlatformServices
from .tools import ExtensionTools
from .development import DevelopmentApplications
from .errors import InvalidRequestError, NotFoundError, ConflictError
from .extension_artifacts import prepare_artifact, validate_import_request
from f8pysdk.application_spec import ApplicationEndpoint
from f8pysdk.application_package import read_application_extension
from f8pysdk.extension_status import ExtensionImportRequest
from f8pysdk.extension_status import RuntimeStorageStatus
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from f8pysdk.management_job import ManagementJob, ManagementJobRequest, ManagementJobLog
from .management_jobs import ManagementJobs


class PlatformRuntime:
    def __init__(self, data_dir: Path, *, source_index: Path | None = None,
                 development: Path | None = None, applications: ApplicationManager | None = None) -> None:
        self.data_dir = data_dir
        self.application_operations: dict[str, ApplicationOperation] = {}
        self.application_tasks: dict[str, asyncio.Task[None]] = {}
        self.applications = applications or ApplicationManager(data_dir)
        self.extensions = ExtensionManager(data_dir, base_index=source_index or data_dir / 'distribution/config/service-index.json')
        self.tools = ExtensionTools(self.extensions, data_dir / 'tool-jobs')
        self.services = PlatformServices(self.extensions, ServiceCatalog())
        catalog = msgspec.json.decode(development.read_bytes(), type=DevelopmentCatalog) if development else DevelopmentCatalog(applications=())
        self.development = DevelopmentApplications(data_dir, catalog, self.applications)
        self.applications.require_external_dependents_stopped = self.development.require_dependents_stopped
        self.applications.require_source_stopped = self.development.require_source_stopped
        self.sync_application_environments()
        self.refresh()
        self.jobs = ManagementJobs(data_dir / 'management-jobs', self.execute_management_job, self.cancel_job_operation,
                                   self.job_progress, self.job_cancellable)

    def job_cancellable(self, request: ManagementJobRequest) -> bool:
        return request.action == 'prepare-environment' or (request.action == 'install-extension'
            and request.extension_id is not None and not self.status(request.extension_id).application)

    def submit_job(self, request: ManagementJobRequest) -> ManagementJob:
        release_actions = {'prepare-application', 'select-application', 'update-application', 'uninstall-application'}
        if request.action == 'import-application':
            if (not request.location or not request.sha256 or request.extension_id is not None
                    or request.environment_id is not None or request.package is not None):
                raise InvalidRequestError('Application import requires only a location and SHA-256')
            return self.jobs.submit(request)
        if request.action in release_actions:
            if (not request.extension_id or not request.sha256 or request.location is not None
                    or request.environment_id is not None or request.package is not None):
                raise InvalidRequestError('Application release tasks require only an extension ID and SHA-256')
            self.status(request.extension_id)
            return self.jobs.submit(request)
        if request.sha256 is not None or request.location is not None:
            raise InvalidRequestError('This task does not accept an application archive or digest')
        if request.action == 'import-extension':
            if request.package is None or request.extension_id is not None or request.environment_id is not None:
                raise InvalidRequestError('Import requires only a package URL and SHA-256')
            validate_import_request(request.package)
        elif request.action in {'prepare-environment', 'remove-environment'}:
            if request.environment_id is None or request.extension_id is not None or request.package is not None:
                raise InvalidRequestError('This task requires only an environment ID')
            self.extensions.runtime_registry.source(request.environment_id)
        elif request.action == 'clean-unused-environments':
            if request.extension_id is not None or request.environment_id is not None or request.package is not None:
                raise InvalidRequestError('Unused environment cleanup takes no arguments')
        else:
            if request.extension_id is None or request.environment_id is not None or request.package is not None:
                raise InvalidRequestError('This task requires only an extension ID')
            status = self.status(request.extension_id)
            if request.action == 'install-extension' and status.application and status.release_sha256 is None:
                raise InvalidRequestError('This is a source checkout. Use Start source, or import a published release package.')
            if request.action == 'start-source' and request.extension_id not in self.development.definitions:
                raise NotFoundError(f'Unknown source application: {request.extension_id}')
        return self.jobs.submit(request)

    async def execute_management_job(self, request: ManagementJobRequest) -> None:
        try:
            await self.execute_job(request)
        finally:
            self.extensions.runtime_registry.invalidate_usage()

    async def execute_job(self, request: ManagementJobRequest) -> None:
        extension_id = request.extension_id
        environment_id = request.environment_id
        match request.action:
            case 'import-extension':
                assert request.package is not None
                await self.import_package(request.package)
            case 'install-extension':
                assert extension_id is not None
                application = self.status(extension_id).application
                await self.install(extension_id)
                if not application:
                    await self.extensions.wait_installation(extension_id)
            case 'uninstall-extension':
                assert extension_id is not None
                await self.uninstall(extension_id)
            case 'enable-extension' | 'disable-extension':
                assert extension_id is not None
                await self.set_enabled(extension_id, request.action == 'enable-extension')
            case 'prepare-environment':
                assert environment_id is not None
                await self.prepare_environment(environment_id)
                await self.extensions.wait_preparation(environment_id)
            case 'remove-environment':
                assert environment_id is not None
                await asyncio.to_thread(self.remove_environment, environment_id)
            case 'clean-unused-environments':
                await self.clean_unused_environments()
            case 'start-source':
                assert extension_id is not None
                await self.development.start(extension_id)
            case 'start-application':
                assert extension_id is not None
                await self.development.probe_observed()
                if any(item.extension_id == extension_id and item.state == 'running' for item in self.development.statuses()):
                    raise ConflictError('Stop the source application before starting an installed release')
                await self.applications.start(extension_id)
            case 'import-application':
                assert request.location is not None and request.sha256 is not None
                await self.applications.import_archive(request.location, request.sha256)
                self.sync_application_environments()
            case 'prepare-application':
                assert extension_id is not None and request.sha256 is not None
                await self.applications.prepare(extension_id, request.sha256)
            case 'select-application':
                assert extension_id is not None and request.sha256 is not None
                await self.applications.select(extension_id, request.sha256)
            case 'deselect-application':
                assert extension_id is not None
                await self.applications.deselect(extension_id)
            case 'update-application':
                assert extension_id is not None and request.sha256 is not None
                await self.applications.update(extension_id, request.sha256)
            case 'uninstall-application':
                assert extension_id is not None and request.sha256 is not None
                await self.applications.uninstall(extension_id, request.sha256)

    async def cancel_job_operation(self, request: ManagementJobRequest) -> bool:
        if request.action == 'install-extension':
            assert request.extension_id is not None
            if not self.extensions.installation_running(request.extension_id):
                return False
            await self.extensions.cancel(request.extension_id)
            return True
        elif request.action == 'prepare-environment':
            assert request.environment_id is not None
            if not self.extensions.runtime_registry.preparation_running(request.environment_id):
                return False
            await self.extensions.runtime_registry.cancel(request.environment_id)
            return True
        return False

    def job_progress(self, request: ManagementJobRequest) -> str:
        if request.action == 'install-extension' and request.extension_id is not None:
            return self.status(request.extension_id).detail
        if request.action == 'prepare-environment' and request.environment_id is not None:
            return self.extensions.runtime_registry.status(request.environment_id).detail
        return ''

    async def cancel_matching_job(self, action: str, identifier: str) -> ManagementJob:
        job = next((job for job in self.jobs.list() if job.state in {'queued', 'running'}
                    and job.request.action == action
                    and identifier in {job.request.extension_id, job.request.environment_id}), None)
        if job is None:
            raise NotFoundError('No active maintenance task matches this request')
        return await self.jobs.cancel(job.job_id)

    def job_log(self, identifier: str) -> ManagementJobLog:
        job = self.jobs.get(identifier)
        paths = [self.jobs.root / f'{identifier}.log']
        if job.request.action == 'install-extension':
            paths.append(self.data_dir / 'extensions/logs' / f'{job.request.extension_id}.log')
        elif job.request.action == 'prepare-environment':
            paths.append(self.extensions.runtime_registry.definitions / 'logs' / f'{job.request.environment_id}.log')
        elif job.request.action == 'start-source':
            paths.append(self.data_dir / 'source-logs' / f'{job.request.extension_id}.log')
        if job.request.action in {'install-extension', 'prepare-application', 'start-application', 'update-application'}:
            paths.append(self.data_dir / 'applications/logs' / f'{job.request.extension_id}.log')
        output: list[str] = []
        for path in paths:
            if path.is_file():
                with path.open('rb') as log:
                    log.seek(max(0, path.stat().st_size - 128 * 1024))
                    output.append(log.read().decode('utf-8', errors='replace'))
        return ManagementJobLog(log='\n'.join(output))

    def sync_application_environments(self) -> None:
        for manager, request in self.applications.environment_sources():
            self.extensions.runtime_registry.add_source(manager, request, official=False)

    def environment_statuses(self) -> tuple[EnvironmentStatus, ...]:
        self.sync_application_environments()
        consumers: dict[str, set[str]] = {}
        for manager, request in self.applications.environment_sources():
            identifier = manager.plan(request).environment_id
            if identifier is not None:
                consumers.setdefault(identifier, set()).add(request.extension_id)
        return tuple(copy_model(status, update={'extension_ids': tuple(sorted(
            {*status.extension_ids, *consumers.get(status.environment_id, ())})),
            'can_remove': status.can_remove and not consumers.get(status.environment_id)})
            for status in self.extensions.environment_statuses())

    def referenced_environment_ids(self) -> set[str]:
        referenced = self.extensions.referenced_environment_ids()
        for item in self.applications.list():
            if item.prepared or item.selected:
                identifier = self.applications.environment_plan(item.manifest.extension_id, item.sha256).environment_id
                if identifier is not None:
                    referenced.add(identifier)
        return referenced

    def storage_status(self, *, refresh: bool = False) -> RuntimeStorageStatus:
        self.sync_application_environments()
        status = self.extensions.runtime_registry.storage_status(self.referenced_environment_ids(), refresh=refresh)
        if self.applications.runtime_busy or self.extensions.runtime_busy:
            return copy_model(status, update={'can_change': False})
        return status

    async def clean_unused_environments(self) -> None:
        async with self.applications.runtime_maintenance():
            await asyncio.to_thread(self.extensions.clean_unused_environments, self.referenced_environment_ids())

    async def prepare_environment(self, identifier: str) -> EnvironmentStatus:
        environment = next((item for item in self.environment_statuses() if item.environment_id == identifier), None)
        if environment is None:
            raise NotFoundError(f'Unknown environment: {identifier}')
        if any(self.status(name).application and self.status(name).running for name in environment.extension_ids):
            raise ConflictError('Stop applications using this environment before preparing it')
        return await self.extensions.prepare_environment(identifier, self.refresh, self.services.is_class_running)

    def remove_environment(self, identifier: str) -> None:
        environment = next((item for item in self.environment_statuses() if item.environment_id == identifier), None)
        if environment is not None and any(self.status(name).application and self.status(name).release_sha256
                                           for name in environment.extension_ids):
            raise ConflictError('Uninstall applications using this environment before removing it')
        self.extensions.remove_environment(identifier)

    def refresh(self) -> PlatformInventory:
        catalog = ServiceCatalog()
        for index in self.extensions.active_indexes():
            load_index_into_catalog(path=index, catalog=catalog)
        self.services.catalog = catalog
        describes = tuple(F8ServiceDescribe(service=service, operators=list(
            operator for operator in catalog.operators.all() if operator.serviceClass == service.serviceClass
        )) for service in catalog.services.all())
        skills = {name: path.read_text(encoding='utf-8') for name, path in self.extensions.active_skill_files().items()}
        return PlatformInventory(describes=describes, skills=skills,
                                 service_classes=tuple(str(service.serviceClass) for service in catalog.services.all()))

    def statuses(self) -> tuple[ExtensionStatus, ...]:
        applications: dict[str, ApplicationStatus] = {}
        for item in self.applications.list():
            previous = applications.get(item.manifest.extension_id)
            if previous is None or item.selected:
                applications[item.manifest.extension_id] = item
        sources = {item.extension_id: item for item in self.development.statuses()}
        statuses: list[ExtensionStatus] = []
        for status in self.extensions.statuses():
            manifest = self.extensions.manifest(status.extension_id)
            if manifest.application is None:
                statuses.append(status)
                continue
            installed = applications.pop(status.extension_id, None)
            source = self.development.definitions.get(status.extension_id)
            statuses.append(copy_model(status, update={
                'application': True, 'state': ('installed' if installed.selected else 'disabled') if installed and installed.prepared else 'available',
                'version': installed.manifest.version if installed else status.version,
                'source_checkout': status.source_checkout or source is not None,
                'source_path': status.source_path or (str(Path(source.runtime_manifest).parent) if source else None),
                'running': (installed is not None and installed.state == 'running') or (status.extension_id in sources and sources[status.extension_id].state == 'running'),
                'running_source': status.extension_id in sources and sources[status.extension_id].state == 'running',
                'managed': (sources[status.extension_id].managed if status.extension_id in sources and sources[status.extension_id].state == 'running'
                            else installed is not None or (status.extension_id in sources and sources[status.extension_id].managed)),
                'release_sha256': installed.sha256 if installed else status.release_sha256,
            }))
        for item in applications.values():
            statuses.append(ExtensionStatus(extension_id=item.manifest.extension_id, name=item.manifest.title,
                version=item.manifest.version, description='Application extension',
                state=('installed' if item.selected else 'disabled') if item.prepared else 'available', detail='', service_classes=(), runtime_kind='pixi', environment_id=None,
                preinstalled=False, application=True, running=item.state == 'running', managed=True,
                release_sha256=item.sha256))
        return tuple(copy_model(status, update={'application_operation': self.application_operations.get(status.extension_id)})
                     for status in statuses)

    def stop_application(self, extension_id: str, *, source: bool) -> ApplicationOperation:
        if source and extension_id not in self.development.definitions:
            raise NotFoundError(f'Unknown source application: {extension_id}')
        if source and extension_id in self.development.observed:
            raise ConflictError('This source application is externally managed; stop it at its original entrypoint')
        self.development.require_dependents_stopped(extension_id)
        status = self.status(extension_id)
        if not status.application or (status.running and not status.managed):
            raise ConflictError('This application is externally managed; stop it at its original entrypoint')
        previous = self.application_tasks.get(extension_id)
        if previous is not None and not previous.done():
            raise ConflictError('An application operation is already running')
        operation = ApplicationOperation(extension_id=extension_id, action='stop', state='running')
        self.application_operations[extension_id] = operation

        async def stop() -> None:
            try:
                if source:
                    await self.development.stop(extension_id)
                else:
                    await self.applications.stop(extension_id)
                self.application_operations[extension_id] = copy_model(operation, update={'state': 'succeeded'})
            except (OSError, ValueError, RuntimeError):
                logging.getLogger(__name__).exception('Cannot stop application %s', extension_id)
                self.application_operations[extension_id] = copy_model(operation, update={
                    'state': 'failed', 'detail': 'Application stop failed; see the platform console log.'})
        self.application_tasks[extension_id] = asyncio.create_task(stop(), name=f'stop-application:{extension_id}')
        return operation

    def status(self, extension_id: str) -> ExtensionStatus:
        status = next((item for item in self.statuses() if item.extension_id == extension_id), None)
        if status is None:
            raise NotFoundError(f'Unknown extension: {extension_id}')
        return status

    def detail(self, extension_id: str) -> ExtensionDetail:
        status = self.status(extension_id)
        if not status.application:
            return self.extensions.detail(extension_id)
        released = next((item for item in self.applications.list() if item.manifest.extension_id == extension_id
                         and item.sha256 == status.release_sha256), None)
        source = self.development.definitions.get(extension_id)
        manifest = released.manifest if released else source.manifest if source else None
        return ExtensionDetail(extension_id=extension_id, services=(), tools=(), skills=(), application=manifest)

    def install_plan(self, extension_id: str) -> ExtensionInstallPlan:
        status = self.status(extension_id)
        if not status.application:
            return self.extensions.install_plan(extension_id)
        if status.release_sha256 is None:
            return ExtensionInstallPlan(extension_id=extension_id, environment_id=None, runtime_kind='workspace',
                                        action='workspace', requires_network=False)
        return self.applications.environment_plan(extension_id, status.release_sha256)

    async def dependency_endpoints(self, extension_id: str, *, start: bool) -> tuple[ApplicationEndpoint, ...]:
        source = self.development.definitions.get(extension_id)
        releases = {item.manifest.extension_id: item for item in self.applications.list() if item.selected}
        release = releases.get(extension_id)
        manifest = release.manifest if release is not None else source.manifest if source is not None else None
        if manifest is None:
            raise NotFoundError(f'Unknown application: {extension_id}')
        result: list[ApplicationEndpoint] = []
        for dependency in manifest.requires:
            provider = self.development.definitions.get(dependency.extension_id) if source is not None else None
            selected = releases.get(dependency.extension_id)
            provider_manifest = selected.manifest if selected is not None else provider.manifest if provider is not None else None
            if provider_manifest is None or not any(protocol.protocol_id == dependency.protocol_id
                and Version(protocol.version) in SpecifierSet(dependency.versions) for protocol in provider_manifest.provides):
                raise ConflictError(f'Incompatible application dependency: {dependency.extension_id}')
            if selected is not None:
                if start:
                    await self.applications.start(dependency.extension_id)
                endpoints = selected.endpoints
            elif provider is not None:
                if start:
                    await self.development.start(dependency.extension_id)
                endpoints = provider.manifest.endpoints
            else:
                raise ConflictError(f'Missing application dependency: {dependency.extension_id}')
            result.extend(ApplicationEndpoint(name=f'{dependency.extension_id}.{endpoint.name}', url=endpoint.url)
                          for endpoint in endpoints)
        return tuple(result)

    async def import_package(self, request: ExtensionImportRequest) -> tuple[ExtensionStatus, ...]:
        payload = await asyncio.to_thread(prepare_artifact, request, self.data_dir / 'imports')
        catalog = msgspec.json.decode((payload / 'config/extensions.json').read_bytes(), type=ExtensionCatalog)
        if any(item.application is not None for item in catalog.extensions):
            read_application_extension(payload)
            archive = self.data_dir / 'imports/packages' / f'{request.sha256}.zip'
            await self.applications.import_archive(str(archive.resolve()), request.sha256)
            self.sync_application_environments()
        else:
            await self.extensions.import_package(request)
        return self.statuses()

    async def install(self, extension_id: str) -> ExtensionStatus:
        status = self.status(extension_id)
        if not status.application:
            return await self.extensions.install(extension_id, self.refresh)
        if status.release_sha256 is None:
            raise InvalidRequestError('This is a source checkout. Use Start source, or import a published release package.')
        await self.applications.prepare(extension_id, status.release_sha256)
        await self.applications.select(extension_id, status.release_sha256)
        return self.status(extension_id)

    async def set_enabled(self, extension_id: str, enabled: bool) -> ExtensionStatus:
        status = self.status(extension_id)
        if not status.application:
            return await self.extensions.set_enabled(extension_id, enabled, self.refresh, self.services.is_class_running)
        if status.release_sha256 is None:
            raise InvalidRequestError('Import a release before enabling it')
        if enabled:
            await self.applications.select(extension_id, status.release_sha256)
        else:
            await self.applications.deselect(extension_id)
        return self.status(extension_id)

    async def uninstall(self, extension_id: str) -> ExtensionStatus:
        status = self.status(extension_id)
        if not status.application:
            return await self.extensions.uninstall(extension_id, self.refresh, self.services.is_class_running)
        if status.running:
            raise ConflictError('Stop the application before uninstalling')
        if status.release_sha256 is not None:
            await self.applications.uninstall(extension_id, status.release_sha256)
        return copy_model(status, update={'state': 'available', 'release_sha256': None, 'running': False})

    async def close(self) -> None:
        await self.jobs.close()
        if self.application_tasks:
            await asyncio.gather(*self.application_tasks.values())
        await self.development.close()
        await self.applications.close()
        await self.services.close()
        await self.tools.close()
        await self.extensions.close()
