from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
import msgspec
import pytest
import yaml

from f8platform.api import access_token, create_app
from f8platform.applications import ApplicationManager
from f8platform.environments import EnvironmentManager
from f8platform.errors import ConflictError, InvalidRequestError, ServiceUnavailableError
from f8pysdk.application_package import validate_application
from f8pysdk.application_spec import (
    ApplicationDependency, ApplicationEndpoint, ApplicationHealth, ApplicationLaunch, ApplicationSpec, ApplicationProtocol,
)
from f8pysdk.release_spec import PublishedArtifact


def artifact(root: Path, name: str, *, version: str = '1.0', protocol: str = '1.0',
             requires: tuple[ApplicationDependency, ...] = (), web: bool = False, port: int = 19431) -> tuple[Path, str]:
    payload = root / f'{name}-{version}'
    (payload / 'config').mkdir(parents=True)
    workspace = payload / 'workspace'
    workspace.mkdir()
    (workspace / 'pixi.toml').write_text('[workspace]\nname="fixture"\nchannels=["conda-forge"]\nplatforms=["linux-64"]\n'
                                      '[dependencies]\npython="3.14.*"\n[environments]\nruntime=[]\n')
    package = {'conda': 'https://example.invalid/python-3.14.0-build_0.conda', 'sha256': 'a' * 64}
    (workspace / 'pixi.lock').write_text(yaml.safe_dump({'version': 6, 'environments': {
        'runtime': {'packages': {'linux-64': [{'conda': package['conda']}]}}}, 'packages': [package]}))
    (payload / 'config/runtime-environments.json').write_text(json.dumps({'schemaVersion': 'f8runtimeCatalog/1', 'runtimes': [{
        'runtimeId': 'runtime', 'providerId': name, 'version': version, 'manifest': '${F8_PACKAGE_ROOT}/workspace/pixi.toml',
    }]}))
    application = ApplicationSpec(
        launch=ApplicationLaunch(environment='runtime', module='fixture', distribution='fixture', args=(str(port), name, version)),
        provides=(ApplicationProtocol(protocol_id='api', version=protocol),), requires=requires,
        endpoints=(ApplicationEndpoint(name='http', url=f'http://127.0.0.1:{port}'),),
        health=ApplicationHealth(endpoint='http', path='/health', service=name, protocol_version='api/1', timeout_seconds=3),
        release_role='webstudio' if web else 'application', web_assets='${F8_PACKAGE_ROOT}/web' if web else None)
    from f8pysdk.extension_spec import ExtensionCatalog, ExtensionManifest, ExtensionRuntime
    manifest = ExtensionManifest(extension_id=name, name=name, version=version, description='Application fixture',
                                 runtime=ExtensionRuntime(kind='pixi', environment='runtime'), application=application)
    catalog = ExtensionCatalog(schema_version='f8extensionCatalog/1', extensions=(manifest,))
    (payload / 'extension.json').write_bytes(msgspec.json.encode(catalog))
    (payload / 'config/extensions.json').write_bytes(msgspec.json.encode(catalog))
    (payload / 'config/service-index.json').write_text('{"schemaVersion":"f8serviceIndex/1","services":[],"modelRoot":"${F8_MODEL_ROOT}"}')
    (payload / 'config/artifact.json').write_bytes(msgspec.json.encode(PublishedArtifact(schema_version='f8artifact/1', artifact_id=name,
        version=version, kind='extension', platform='windows-x86_64' if sys.platform == 'win32' else 'linux-x86_64')))
    if web:
        (payload / 'web').mkdir()
        (payload / 'web/index.html').write_text('frontend')
        (payload / 'web/f8-release.json').write_text(json.dumps({'extensionId': name, 'version': version}))
    output = root / f'{name}-{version}.zip'
    with zipfile.ZipFile(output, 'w') as archive:
        for path in payload.rglob('*'):
            if path.is_file():
                archive.write(path, path.relative_to(payload).as_posix())
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


def requires(name: str, versions: str = '>=1,<2') -> tuple[ApplicationDependency, ...]:
    return (ApplicationDependency(extension_id=name, protocol_id='api', versions=versions),)


def test_independent_prefixes_share_cache(tmp_path: Path) -> None:
    manager = ApplicationManager(tmp_path / 'data')
    records = [manager.import_local_archive(str(path.resolve()), digest)
               for path, digest in (artifact(tmp_path, 'first'), artifact(tmp_path, 'second'))]
    runtimes = [manager._runtime(record) for record in records]
    plans = [runtime.plan(request) for _manifest, runtime, request in runtimes]
    assert plans[0].environment_id != plans[1].environment_id
    assert runtimes[0][1].install_environment()['PIXI_CACHE_DIR'] == runtimes[1][1].install_environment()['PIXI_CACHE_DIR']


def test_webstudio_requires_matching_frontend(tmp_path: Path) -> None:
    artifact(tmp_path, 'studio', web=True)
    root = tmp_path / 'studio-1.0'
    assert validate_application(root).release_role == 'webstudio'
    (root / 'web/f8-release.json').write_text('{"extensionId":"studio","version":"2.0"}')
    with pytest.raises(ValueError, match='same application release'):
        validate_application(root)
    (root / 'web/index.html').unlink()
    with pytest.raises(ValueError, match='include index.html'):
        validate_application(root)


def test_archive_checksum_is_required(tmp_path: Path) -> None:
    path, _digest = artifact(tmp_path, 'provider')
    manager = ApplicationManager(tmp_path / 'data')
    with pytest.raises(InvalidRequestError, match='checksum'):
        manager.import_local_archive(str(path.resolve()), '0' * 64)
    assert not manager.state.records


def test_missing_incompatible_and_cyclic_dependencies(tmp_path: Path) -> None:
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        client, digest = artifact(tmp_path, 'client', requires=requires('provider'))
        await manager.import_archive(str(client.resolve()), digest)
        with pytest.raises(ConflictError, match='requires selected'):
            await manager.select('client', digest)
        provider, provider_digest = artifact(tmp_path, 'provider', protocol='2.0')
        await manager.import_archive(str(provider.resolve()), provider_digest)
        await manager.select('provider', provider_digest)
        with pytest.raises(ConflictError, match='protocol'):
            await manager.select('client', digest)
        cyclic, cyclic_digest = artifact(tmp_path, 'cycle', requires=requires('client'))
        await manager.import_archive(str(cyclic.resolve()), cyclic_digest)
        cycle_client, cycle_client_digest = artifact(tmp_path, 'client', version='2.0', requires=requires('cycle'))
        await manager.import_archive(str(cycle_client.resolve()), cycle_client_digest)
        with pytest.raises(ConflictError, match='cycle'):
            await manager.select_all({'client': cycle_client_digest, 'cycle': cyclic_digest})
    asyncio.run(run())


def test_versions_are_immutable_and_rollback_persists(tmp_path: Path) -> None:
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        records = []
        for version in ('1.0', '1.1'):
            path, digest = artifact(tmp_path, 'provider', version=version)
            records.append(await manager.import_archive(str(path.resolve()), digest))
        for record in (records[0], records[1], records[0]):
            await manager.select(record.extension_id, record.sha256)
        restored = ApplicationManager(tmp_path / 'data')
        assert restored.state.selected == {'provider': records[0].sha256}
        with zipfile.ZipFile(tmp_path / 'provider-1.0.zip', 'a') as archive:
            archive.writestr('changed.txt', 'changed')
        path = tmp_path / 'provider-1.0.zip'
        with pytest.raises(ConflictError, match='immutable'):
            await manager.import_archive(str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest())
    asyncio.run(run())


FIXTURE_SERVER = '''
import http.server, json, os, sys
port, service, version = sys.argv[-3:]
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(dict(status='ok', service=service, version=version, protocolVersion='api/1',
                               applicationInstance=os.environ['F8_APPLICATION_INSTANCE'])).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)
http.server.HTTPServer(('127.0.0.1', int(port)), Handler).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


def test_real_lifecycle_checks_dependencies_and_stops_processes(tmp_path: Path) -> None:
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        for name, dependency in (('provider', ()), ('client', requires('provider'))):
            path, digest = artifact(tmp_path, name, requires=dependency, port=free_port())
            await manager.import_archive(str(path.resolve()), digest)
            await manager.select(name, digest)
        with patch.object(EnvironmentManager, 'ready', return_value=True), patch.object(
                EnvironmentManager, 'python_launch', return_value=(sys.executable, ['-c', FIXTURE_SERVER])):
            await manager.start('client')
            assert set(manager.running) == {'provider', 'client'}
            assert all(item.state == 'running' for item in manager.list())
            with pytest.raises(ConflictError, match='dependent'):
                await manager.stop('provider')
            with pytest.raises(ConflictError, match='Stop running'):
                await manager.select('provider', manager.state.selected['provider'])
            await manager.stop('client')
            processes = [item.process for item in manager.running.values()]
            await manager.close()
            assert not manager.running
            assert all(process.returncode is not None for process in processes)
    asyncio.run(run())


def test_failed_health_rolls_back_started_processes(tmp_path: Path) -> None:
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        path, digest = artifact(tmp_path, 'provider', port=free_port())
        await manager.import_archive(str(path.resolve()), digest)
        await manager.select('provider', digest)
        mismatch = FIXTURE_SERVER.replace("protocolVersion='api/1'", "protocolVersion='api/2'")
        with patch.object(EnvironmentManager, 'ready', return_value=True), patch.object(
                EnvironmentManager, 'python_launch', return_value=(sys.executable, ['-c', mismatch])):
            with pytest.raises(ServiceUnavailableError, match='mismatch'):
                await manager.start('provider')
            assert not manager.running
    asyncio.run(run())


def test_platform_api_requires_token_and_reports_errors(tmp_path: Path) -> None:
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/applications').status_code == 401
        headers = {'Authorization': f'Bearer {access_token(tmp_path)}'}
        assert client.get('/api/health', headers=headers).json()['service'] == 'f8platform'
        assert client.get('/api/applications', headers=headers).json() == []
        assert client.post('/api/applications/missing/start', headers=headers).status_code == 404


def test_update_failure_restores_running_release(tmp_path: Path) -> None:
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        endpoint_port = free_port()
        records = []
        for version in ('1.0', '1.1'):
            path, digest = artifact(tmp_path, 'provider', version=version, port=endpoint_port)
            records.append(await manager.import_archive(str(path.resolve()), digest))
        await manager.select('provider', records[0].sha256)
        mismatch = FIXTURE_SERVER.replace("protocolVersion='api/1'", "protocolVersion='api/2' if version == '1.1' else 'api/1'")
        with patch.object(EnvironmentManager, 'ready', return_value=True), patch.object(EnvironmentManager, 'ensure'), patch.object(
                EnvironmentManager, 'python_launch', return_value=(sys.executable, ['-c', mismatch])):
            try:
                await manager.start('provider')
                with pytest.raises(ServiceUnavailableError, match='mismatch'):
                    await manager.update('provider', records[1].sha256)
                assert manager.state.selected['provider'] == records[0].sha256
                assert manager.running['provider'].process.returncode is None
            finally:
                await manager.close()
    asyncio.run(run())


def test_endpoint_configuration_is_scoped_and_persistent(tmp_path: Path) -> None:
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        path, digest = artifact(tmp_path, 'provider')
        await manager.import_archive(str(path.resolve()), digest)
        await manager.select('provider', digest)
        url = f'http://127.0.0.1:{free_port()}'
        await manager.configure('provider', {'http': url})
        assert manager.list()[0].endpoints[0].url == url
        restored = ApplicationManager(tmp_path / 'data')
        assert restored.list()[0].endpoints[0].url == url
        with pytest.raises(InvalidRequestError, match='loopback'):
            await manager.configure('provider', {'http': 'http://example.com:8210'})
        with pytest.raises(InvalidRequestError, match='undeclared'):
            await manager.configure('provider', {'missing': url})
    asyncio.run(run())


def test_only_one_platform_owns_data_directory(tmp_path: Path) -> None:
    from f8platform.instance import single_platform_instance
    with single_platform_instance(tmp_path):
        with pytest.raises(ConflictError, match='already owns'):
            with single_platform_instance(tmp_path):
                pytest.fail('second owner acquired the same platform data')
    with single_platform_instance(tmp_path):
        pass


def test_reopening_distribution_preserves_user_selected_upgrade(tmp_path: Path) -> None:
    from f8platform.distribution import install_distribution
    from f8pysdk.release_spec import BundledExtensionCatalog, BundledExtensionPackage, PlatformReleaseLock
    import shutil
    async def run() -> None:
        manager = ApplicationManager(tmp_path / 'data')
        old_path, old_digest = artifact(tmp_path, 'provider', version='1.0')
        new_path, new_digest = artifact(tmp_path, 'provider', version='1.1')
        await manager.import_archive(str(new_path.resolve()), new_digest)
        await manager.select('provider', new_digest)
        source = tmp_path / 'distribution'
        (source / 'config').mkdir(parents=True)
        (source / 'extension-archives').mkdir()
        payload = source / 'extension-packages' / old_digest
        shutil.copytree(tmp_path / 'provider-1.0', payload)
        shutil.copy2(old_path, source / 'extension-archives' / f'{old_digest}.zip')
        (source / 'config/extension-packages.json').write_bytes(msgspec.json.encode(BundledExtensionCatalog(
            schema_version='f8extensionPackages/1', packages=(BundledExtensionPackage(
                path='${F8_PACKAGE_ROOT}/extension-packages/' + old_digest, sha256=old_digest),))))
        (source / 'config/release-lock.json').write_bytes(msgspec.json.encode(PlatformReleaseLock(
            schema_version='f8platformRelease/1', platform='linux-x86_64', artifacts=(), startup=())))
        (source / 'config/service-index.json').write_text('{"schemaVersion":"f8serviceIndex/1","services":[],"modelRoot":"${F8_MODEL_ROOT}"}')
        with patch.object(ApplicationManager, 'prepare'):
            await install_distribution(manager, source)
        assert manager.state.selected['provider'] == new_digest
    asyncio.run(run())


def test_standalone_platform_initializes_catalog_and_shared_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from f8platform.extensions import ExtensionManager
    manager = ApplicationManager(tmp_path / 'data')
    index = manager.data_dir / 'distribution/config/service-index.json'
    assert index.is_file()
    monkeypatch.setenv('F8_RUNTIME_STORAGE_ROOT', str(manager.data_dir))
    extensions = ExtensionManager(manager.data_dir / 'studio', base_index=index)
    assert extensions.statuses() == ()
    assert extensions.environments.root == manager.data_dir / 'runtimes'
    assert extensions.environments.install_environment()['PIXI_CACHE_DIR'] == str(manager.data_dir / 'package-cache')
