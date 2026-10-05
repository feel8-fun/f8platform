"""Management and task lifetime must work with no WebStudio installation."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx
import msgspec
import pytest

from f8platform.api import access_token, create_app
from f8platform.environments import EnvironmentManager
from f8platform.extension_operation import InstallOperation
from f8pysdk.extension_status import ExtensionManifest, ExtensionInstallPlan
from f8pysdk.platform_client import PlatformClient, PlatformConnection
from f8pysdk.platform_spec import DevelopmentCatalog
from f8pysdk.tool_spec import ToolRunRequest

from test_applications import artifact


def wait_job(client: TestClient, headers: dict[str, str], response: httpx.Response, expected: str = 'succeeded') -> dict[str, object]:
    assert response.status_code == 202, response.text
    identifier = response.json()['jobId']
    deadline = time.monotonic() + 5
    while True:
        job = client.get(f'/api/management-jobs/{identifier}', headers=headers).json()
        if job['state'] not in {'queued', 'running'}:
            assert job['state'] == expected, job
            return job
        assert time.monotonic() < deadline, job
        time.sleep(0.01)


def test_source_dependency_prefers_compatible_configured_release(tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import AsyncMock
    import pytest
    from f8pysdk.application_package import read_application
    from f8pysdk.platform_spec import DevelopmentApplication
    from f8platform.runtime import PlatformRuntime
    from f8platform.errors import ConflictError
    from test_applications import requires

    async def run() -> None:
        provider_archive, digest = artifact(tmp_path, 'provider')
        artifact(tmp_path, 'editor', requires=requires('provider'))
        definitions = tmp_path / 'development.json'
        definitions.write_bytes(msgspec.json.encode(DevelopmentCatalog(applications=(DevelopmentApplication(
            manifest=read_application(tmp_path / 'editor-1.0'), runtime_manifest=str(tmp_path / 'editor-1.0/workspace/pixi.toml'), workdir=str(tmp_path),
            arguments=('-c', 'import sys; print(sys.argv[1], flush=True); sys.stdin.read()',
                       '${F8_ENDPOINT:provider.http}'),
        ),))))
        runtime = PlatformRuntime(tmp_path / 'data', development=definitions)
        try:
            await runtime.applications.import_archive(str(provider_archive), digest)
            await runtime.applications.select('provider', digest)
            await runtime.applications.configure('provider', {'http': 'http://127.0.0.1:19499'})
            with patch.object(runtime.applications, 'start', new_callable=AsyncMock) as start, \
                 patch.object(runtime.applications, 'check_health', new_callable=AsyncMock), \
                 patch.object(runtime.development, '_prepare_runtime', new_callable=AsyncMock, return_value=(sys.executable, [])):
                endpoints = await runtime.dependency_endpoints('editor', start=True)
                assert endpoints[0].url == 'http://127.0.0.1:19499'
                await runtime.development.start('editor')
                start.assert_awaited_with('provider')
                assert runtime.development.expand(('${F8_ENDPOINT:provider.http}',)) == ('http://127.0.0.1:19499',)
                with pytest.raises(ConflictError, match='dependent source'):
                    await runtime.applications.stop('provider')
                with pytest.raises(ConflictError, match='dependent source'):
                    await runtime.applications.configure('provider', {'http': 'http://127.0.0.1:19500'})
                await runtime.development.stop('editor')
            archive, incompatible = artifact(tmp_path, 'provider', version='2.0', protocol='2.0')
            await runtime.applications.import_archive(str(archive), incompatible)
            await runtime.applications.select('provider', incompatible)
            with pytest.raises(ConflictError, match='Incompatible'):
                await runtime.dependency_endpoints('editor', start=False)
            with pytest.raises(ConflictError, match='Incompatible'):
                await runtime.development.start('editor')
        finally:
            await runtime.close()
    asyncio.run(run())


def tool_workspace(root: Path) -> Path:
    config = root / 'config'
    config.mkdir(parents=True)
    script = root / 'stream'
    script.write_text(f'#!{sys.executable}\nimport json, sys, time\njson.load(sys.stdin)\nwhile True:\n print("stream is active",flush=True)\n time.sleep(0.05)\n')
    script.chmod(0o755)
    (root / 'SKILL.md').write_text('Inspect a continuous stream using the diagnostic tools.')
    (root / 'notes.txt').write_text('Reference material')
    (config / 'service-index.json').write_text('{"schemaVersion":"f8serviceIndex/1","services":[],"modelRoot":"${F8_MODEL_ROOT}"}')
    (config / 'extensions.json').write_text(json.dumps({'schemaVersion':'f8extensionCatalog/1','preinstalled':['debug'],
        'extensions':[{'extensionId':'debug','name':'Debug tools','description':'Continuous stream fixture','version':'1.0',
            'runtime':{'kind':'native'},'tools':[{'toolId':'stream','name':'Simulate','description':'Continuous stream',
                'command':'${F8_PACKAGE_ROOT}/stream','timeoutSeconds':None}],
            'skills':[{'skillId':'stream','path':'${F8_PACKAGE_ROOT}/SKILL.md'}],
            'resources':[{'resourceId':'notes','path':'${F8_PACKAGE_ROOT}/notes.txt','description':'Reference'}]}]}))
    return config / 'service-index.json'


@pytest.mark.parametrize('fail_first', [False, True])
def test_install_queue_waits_for_background_installer_and_survives_client_disconnect(tmp_path: Path, fail_first: bool) -> None:
    from threading import Event
    entered, release = Event(), Event()
    index = tool_workspace(tmp_path / 'source')
    catalog_path = index.with_name('extensions.json')
    catalog = json.loads(catalog_path.read_text())
    catalog['preinstalled'] = []
    catalog['extensions'].append({**catalog['extensions'][0], 'extensionId': 'second', 'name': 'Second toolset'})
    catalog_path.write_text(json.dumps(catalog))
    original = EnvironmentManager.ensure
    executions: list[str] = []

    def ensure(manager: EnvironmentManager, manifest: ExtensionManifest, operation: InstallOperation | None = None) -> ExtensionInstallPlan:
        executions.append(manifest.extension_id)
        if manifest.extension_id == 'debug':
            assert operation is not None
            operation.report('Downloading fixture dependencies')
            entered.set()
            assert release.wait(5)
            if fail_first:
                raise ValueError('Fixture dependency resolution failed')
        return original(manager, manifest, operation)

    data = tmp_path / 'data'
    with TestClient(create_app(data, source_index=index)) as client, patch.object(EnvironmentManager, 'ensure', new=ensure):
        headers = {'Authorization': f'Bearer {access_token(data)}'}
        first = client.post('/api/extensions/debug/install', headers=headers)
        try:
            assert first.status_code == 202 and entered.wait(5)
            disconnected = sdk_client(data, client)
            from f8pysdk.management_job import ManagementJobRequest
            second_job = disconnected.jobs.submit(ManagementJobRequest(action='install-extension', extension_id='second'))
            disconnected.close()
            assert second_job.state == 'queued'
            assert client.get('/api/management-jobs/' + first.json()['jobId'], headers=headers).json()['detail'] == 'Downloading fixture dependencies'
            assert executions == ['debug']
            cancelled = client.delete('/api/extensions/debug', headers=headers).json()
            assert client.post('/api/management-jobs/' + cancelled['jobId'] + '/cancel', headers=headers).status_code == 202
        finally:
            release.set()
        wait_job(client, headers, first, 'failed' if fail_first else 'succeeded')
        wait_job(client, headers, httpx.Response(202, json={'jobId': second_job.job_id}))
        assert executions == ['debug', 'second']
        statuses = {item['extensionId']: item for item in client.get('/api/extensions', headers=headers).json()}
        assert statuses['second']['state'] == 'installed'
        assert statuses['debug']['state'] == ('failed' if fail_first else 'installed')


def test_source_environment_failure_does_not_fall_back_to_launcher(tmp_path: Path) -> None:
    import asyncio
    import pytest
    from f8pysdk.application_package import read_application
    from f8pysdk.platform_spec import DevelopmentApplication
    from f8platform.runtime import PlatformRuntime

    artifact(tmp_path, 'source')
    source = tmp_path / 'source-1.0'
    definitions = tmp_path / 'development.json'
    definitions.write_bytes(msgspec.json.encode(DevelopmentCatalog(applications=(DevelopmentApplication(
        manifest=read_application(source), runtime_manifest=str(source / 'workspace/pixi.toml'),
        workdir=str(source), arguments=('-c', 'raise AssertionError("Launcher interpreter was used")'),
    ),))))

    async def run() -> None:
        runtime = PlatformRuntime(tmp_path / 'data', development=definitions)
        try:
            with pytest.raises(RuntimeError, match='Cannot prepare source environment runtime'):
                await runtime.development.start('source')
            assert runtime.development.running == {}
            log = tmp_path / 'data/source-logs/source.log'
            assert log.read_text()
            # A failed install must close its log handle, including on Windows.
            log.rename(log.with_suffix('.failed.log'))
        finally:
            await runtime.close()

    asyncio.run(run())


def sdk_client(data: Path, portal: TestClient) -> PlatformClient:
    def forward(request: httpx.Request) -> httpx.Response:
        response = portal.request(request.method,str(request.url),headers=request.headers,content=request.read())
        return httpx.Response(response.status_code,content=response.content,headers=response.headers)
    return PlatformClient(PlatformConnection(url='http://testserver',token_file=str(data/'platform-token')),
                          transport=httpx.MockTransport(forward))


def test_headless_tool_outlives_clients_and_can_be_stopped(tmp_path: Path) -> None:
    data = tmp_path/'data'
    app = create_app(data,source_index=tool_workspace(tmp_path/'source'))
    with TestClient(app) as http:
        headers = {'Authorization': f'Bearer {access_token(data)}'}
        status = http.get('/api/extensions', headers=headers).json()[0]
        assert status['sourceCheckout'] and status['state'] == 'installed'
        assert status['sourcePath'] == str(tmp_path / 'source')
        assert status['releaseSha256'] is None
        first = sdk_client(data,http)
        assert first.inventory().skills == {'debug:stream':'Inspect a continuous stream using the diagnostic tools.'}
        job = first.tools.submit('debug','stream',ToolRunRequest(arguments={},confirm=True))
        first.close()
        second = sdk_client(data,http)
        deadline = time.monotonic()+5
        while second.tools.get(job.job_id).status == 'queued':
            assert time.monotonic()<deadline
            time.sleep(0.01)
        assert second.tools.get(job.job_id).status == 'running'
        assert second.tools.read_resource('debug','notes').content == 'Reference material'
        headers={'Authorization':f'Bearer {access_token(data)}'}
        wait_job(http, headers, http.put('/api/extensions/debug/enabled',json={'enabled':False},headers=headers), 'failed')
        wait_job(http, headers, http.delete('/api/extensions/debug',headers=headers), 'failed')
        assert http.post(f'/api/tool-jobs/{job.job_id}/cancel',headers=headers).json()['status'] == 'cancelled'
        wait_job(http, headers, http.put('/api/extensions/debug/enabled',json={'enabled':False},headers=headers))
        second.close()
    with TestClient(create_app(data,source_index=tmp_path/'source/config/service-index.json')) as restored:
        headers={'Authorization':f'Bearer {access_token(data)}'}
        assert restored.get('/api/tool-jobs',headers=headers).json()[0]['status'] == 'cancelled'
        assert restored.get('/api/extensions',headers=headers).json()[0]['state'] == 'disabled'


def test_portal_works_without_studio_and_protects_cookie_operations(tmp_path: Path) -> None:
    app = create_app(tmp_path,url='http://testserver')
    with TestClient(app) as client:
        assert client.get('/').status_code == 200
        assert 'Feel8 Platform' in client.get('/').text
        assert client.get('/api/extensions').status_code == 401
        assert client.get('/bootstrap/incorrect').status_code == 401
        assert client.get('/bootstrap/'+access_token(tmp_path)).status_code == 200
        assert client.get('/api/extensions').json() == []
        assert client.put('/api/startup',json={'applications':[]}).status_code == 403
        assert client.put('/api/startup',json={'applications':[]},headers={'Origin':'https://malicious.invalid'}).status_code == 403
        assert client.put('/api/startup',json={'applications':[]},headers={'Origin':'http://testserver'}).status_code == 200
        assert client.get('/api/extensions',headers={'Sec-Fetch-Site':'cross-site'}).status_code == 403


def test_environment_authoring_endpoints_are_removed(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        headers = {'Authorization': f'Bearer {access_token(tmp_path)}'}
        assert client.post('/api/environments', json={'name': 'custom'}, headers=headers).status_code in {404, 405}
        assert client.get('/api/environments/presets', headers=headers).status_code == 404
        assert client.put('/api/environments/custom/retention', json={'pinned': True}, headers=headers).status_code in {404, 405}
        assert client.put('/api/extensions/custom/runtime', json={'environmentId': 'custom'}, headers=headers).status_code in {404, 405}
        assert client.post('/api/environments/cache/clean', headers=headers).status_code in {404, 405}


def test_storage_api_reuses_measurement_and_explicit_refresh_bypasses_cache(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path)) as client:
        headers = {'Authorization': f'Bearer {access_token(tmp_path)}'}
        cache = tmp_path / 'package-cache'
        cache.mkdir()
        package = cache / 'package'
        package.write_bytes(b'x' * 32)
        first = client.get('/api/environments/storage', headers=headers).json()
        assert first['usageUpdatedAt'] is not None
        package.write_bytes(b'x' * 64)
        cached = client.get('/api/environments/storage', headers=headers).json()
        assert cached['cacheUsage']['uniqueFileBytes'] == 32
        assert cached['usageUpdatedAt'] == first['usageUpdatedAt']
        fresh = client.get('/api/environments/storage?refresh=true', headers=headers).json()
        assert fresh['cacheUsage']['uniqueFileBytes'] == 64


def test_unused_cleanup_keeps_api_responsive_and_queues_extension_install(tmp_path: Path) -> None:
    from threading import Event
    from f8platform.runtime_registry import RuntimeRegistry
    entered, release = Event(), Event()

    def clean(*args: object, **kwargs: object) -> None:
        entered.set()
        assert release.wait(5), 'Platform API was blocked during environment cleanup'

    data = tmp_path / 'data'
    with TestClient(create_app(data, source_index=tool_workspace(tmp_path / 'source'))) as client, \
         patch.object(RuntimeRegistry, 'clean_unused_environments', side_effect=clean):
        headers = {'Authorization': f'Bearer {access_token(data)}'}
        cleanup = client.post('/api/environments/unused/clean', headers=headers)
        try:
            assert cleanup.status_code == 202
            assert entered.wait(5)
            assert client.get('/api/environments', headers=headers).status_code == 200
            assert not client.get('/api/environments/storage', headers=headers).json()['canChange']
            install = client.post('/api/extensions/debug/install', headers=headers)
            assert install.status_code == 202
            assert install.json()['state'] == 'queued'
        finally:
            release.set()
        wait_job(client, headers, cleanup)
        wait_job(client, headers, install)
        assert client.get('/api/environments/storage', headers=headers).json()['canChange']


def test_application_inventory_has_one_installation_authority(tmp_path: Path) -> None:
    archive,digest = artifact(tmp_path,'editor',web=True)
    data=tmp_path/'data'
    app=create_app(data)
    with TestClient(app) as client:
        headers={'Authorization':f'Bearer {access_token(data)}'}
        wait_job(client, headers, client.post('/api/applications/import',json={'location':str(archive),'sha256':digest},headers=headers))
        available=client.get('/api/extensions',headers=headers).json()[0]
        assert available['application'] and not available['sourceCheckout']
        assert available['releaseSha256'] == digest
        assert available['state'] == 'available'
        with patch.object(EnvironmentManager,'ensure'), patch.object(EnvironmentManager,'ready',return_value=True):
            installed=client.post('/api/extensions/editor/install',headers=headers)
            wait_job(client, headers, installed)
            assert client.get('/api/extensions', headers=headers).json()[0]['state'] == 'installed'
            applications=client.get('/api/applications',headers=headers).json()
            assert len(applications) == 1 and applications[0]['selected']
            assert client.get('/api/extensions/editor/detail',headers=headers).json()['application']['extensionId'] == 'editor'
            assert any('editor' in item['extensionIds'] for item in client.get('/api/environments',headers=headers).json())
            wait_job(client, headers, client.put('/api/extensions/editor/enabled',json={'enabled':False},headers=headers))
            assert client.get('/api/extensions', headers=headers).json()[0]['state'] == 'disabled'
            assert not client.get('/api/applications',headers=headers).json()[0]['selected']
        assert not (data/'extensions/state.json').read_text().count('editor')


def test_duplicate_platform_does_not_mutate_owned_state(tmp_path: Path) -> None:
    from f8platform.errors import ConflictError
    import pytest
    first=create_app(tmp_path)
    with TestClient(first):
        snapshot=(tmp_path/'applications/state.json').read_bytes() if (tmp_path/'applications/state.json').exists() else None
        with pytest.raises(ConflictError):
            with TestClient(create_app(tmp_path)):
                raise AssertionError('Duplicate platform acquired ownership')
        assert snapshot == ((tmp_path/'applications/state.json').read_bytes() if (tmp_path/'applications/state.json').exists() else None)
        assert (tmp_path/'platform.json').is_file()


def test_manual_source_execution_is_not_an_installed_release(tmp_path: Path) -> None:
    from f8pysdk.application_package import read_application
    from f8pysdk.platform_spec import DevelopmentApplication
    artifact(tmp_path,'source')
    source=tmp_path/'source-1.0'
    catalog=json.loads((source/'config/extensions.json').read_text())
    catalog['extensions'][0]['runtime']['kind']='workspace'
    (source/'config/extensions.json').write_text(json.dumps(catalog))
    (source/'extension.json').write_text(json.dumps(catalog))
    definitions=tmp_path/'development.json'
    definitions.write_bytes(msgspec.json.encode(DevelopmentCatalog(applications=(DevelopmentApplication(
        manifest=read_application(source), runtime_manifest=str(source / 'workspace/pixi.toml'), workdir=str(source), arguments=('-m','fixture')),))))
    app=create_app(tmp_path/'data',source_index=source/'config/service-index.json',development=definitions)
    alive=True
    def health(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200,json={'status':'ok','version':'1.0','service':'source','protocolVersion':'api/1','applicationInstance':'manual-instance' if alive else 'another-instance'})
    original=httpx.AsyncClient
    def probe_client(**kwargs: object) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(health),**kwargs)
    with TestClient(app) as client, patch('f8platform.development.httpx.AsyncClient',side_effect=probe_client):
        headers={'Authorization':f'Bearer {access_token(tmp_path/"data")}'}
        response=client.post('/api/source-applications/register',headers=headers,json={
            'extensionId':'source','version':'1.0','instance':'manual-instance','url':'http://127.0.0.1:19431'})
        assert response.status_code == 204
        status=client.get('/api/extensions',headers=headers).json()[0]
        assert status['sourceCheckout'] and status['running'] and not status['managed']
        assert status['state']=='available' and status['releaseSha256'] is None
        assert client.get('/api/applications',headers=headers).json()==[]
        assert client.post('/api/extensions/source/install',headers=headers).status_code==400
        assert client.post('/api/source-applications/source/stop',headers=headers).status_code==409
        alive=False
        assert not client.get('/api/extensions',headers=headers).json()[0]['running']
