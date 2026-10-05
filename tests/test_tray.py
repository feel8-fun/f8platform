import subprocess
from pathlib import Path
from unittest import mock
from threading import Event

import pytest
import httpx

from f8platform.tray import stop_process, open_console
from f8platform.tray_endpoints import TrayEndpoint, application_shortcuts, watch_endpoints
from f8pysdk.application_spec import ApplicationEndpoint, ApplicationHealth, ApplicationLaunch, ApplicationManifest
from f8pysdk.platform_spec import ApplicationStatus, SourceApplicationStatus
from f8pysdk.platform_errors import ServiceUnavailableError


def test_tray_exit_closes_parent_pipe_before_waiting() -> None:
    process = mock.Mock(spec=subprocess.Popen)
    process.poll.return_value = None
    process.stdin = mock.Mock()
    stop_process(process)
    process.stdin.close.assert_called_once()
    process.wait.assert_called_once_with(timeout=15)
    process.terminate.assert_not_called()
    process.kill.assert_not_called()


def application(*, running: bool = False, version: str = '1.0') -> ApplicationStatus:
    endpoints = (ApplicationEndpoint(name='browser', url='http://127.0.0.1:8222'),
                 ApplicationEndpoint(name='events', url='ws://127.0.0.1:8222/events'))
    return ApplicationStatus(manifest=ApplicationManifest(extension_id='example', title='Example app',
        version=version, launch=ApplicationLaunch(environment='runtime', module='example', distribution='example'),
        provides=(), endpoints=endpoints, health=ApplicationHealth(endpoint='browser', path='/health',
                                                                 service='example', protocol_version='1')),
        sha256='a' * 64, selected=True, prepared=True, state='running' if running else 'stopped',
        log_path='', endpoints=endpoints)


def test_shortcuts_are_generic_and_include_stopped_installed_apps_and_sources() -> None:
    stopped = application()
    running = application(running=True, version='2.0')
    source = SourceApplicationStatus(extension_id='other', version='1', state='running', log_path='',
        endpoints=(ApplicationEndpoint(name='http', url='http://127.0.0.1:8223'),))
    entries = application_shortcuts((stopped, running), (source,))
    assert len(entries) == 3  # WebSocket endpoints are not browser shortcuts.
    assert entries[0] == TrayEndpoint('Open Example app · v1.0 / browser', 'http://127.0.0.1:8222', False)
    assert entries[1].running is True
    assert entries[2] == TrayEndpoint('Open other (source)', 'http://127.0.0.1:8223', True)


def test_polling_updates_lifecycle_disables_stale_entries_and_removes_uninstalled_apps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = mock.Mock()
    client.read.side_effect = [
        (application(),), (),
        (application(running=True),), (),
        connection_refused(), connection_refused(),
        (), (),
    ]
    stopped = Event()
    snapshots: list[tuple[TrayEndpoint, ...]] = []

    def publish(entries: tuple[TrayEndpoint, ...]) -> None:
        snapshots.append(entries)
        if not entries:
            stopped.set()

    watch_endpoints(client, stopped, publish, interval=0)
    assert [snapshot[0].running if snapshot else None for snapshot in snapshots] == [False, True, False, None]
    assert caplog.text.count('Cannot refresh tray application shortcuts') == 1
    client.close.assert_called_once()


def connection_refused() -> ServiceUnavailableError:
    error = ServiceUnavailableError('Cannot reach platform: connection refused')
    error.__cause__ = httpx.ConnectError('Connection refused')
    return error


def test_startup_waits_for_listener_without_warning_traceback(caplog: pytest.LogCaptureFixture) -> None:
    client = mock.Mock()
    client.read.side_effect = [connection_refused(), connection_refused(), (application(running=True),), ()]
    stopped = Event()
    snapshots: list[tuple[TrayEndpoint, ...]] = []

    def publish(entries: tuple[TrayEndpoint, ...]) -> None:
        snapshots.append(entries)
        stopped.set()

    with caplog.at_level('INFO'):
        watch_endpoints(client, stopped, publish, interval=0)
    assert len(snapshots) == 1 and snapshots[0][0].running
    assert caplog.text.count('Waiting for Platform to start') == 1
    assert 'Platform is ready' in caplog.text
    assert all(record.levelno < 30 and record.exc_info is None for record in caplog.records)
    client.close.assert_called_once()


def test_startup_timeout_reports_failure_instead_of_hiding_it(caplog: pytest.LogCaptureFixture) -> None:
    client = mock.Mock()
    stopped = Event()

    def refuse_connection(*args: object) -> None:
        stopped.set()
        raise connection_refused()

    client.read.side_effect = refuse_connection
    watch_endpoints(client, stopped, mock.Mock(), interval=0, startup_timeout=0)
    assert 'Cannot refresh tray application shortcuts' in caplog.text
    assert 'Platform startup timed out' in caplog.text
    assert caplog.records[0].exc_info is not None
    client.close.assert_called_once()


def test_startup_authentication_error_is_reported_immediately(caplog: pytest.LogCaptureFixture) -> None:
    client = mock.Mock()
    stopped = Event()

    def deny_request(*args: object) -> None:
        stopped.set()
        raise ServiceUnavailableError('Platform token required')

    client.read.side_effect = deny_request
    watch_endpoints(client, stopped, mock.Mock(), interval=0)
    assert 'Cannot refresh tray application shortcuts' in caplog.text
    assert 'Platform token required' in caplog.text
    assert 'Waiting for Platform to start' not in caplog.text
    client.close.assert_called_once()


def test_exit_interrupts_startup_wait_without_publishing_stale_menu() -> None:
    client = mock.Mock()
    stopped = Event()

    def refuse_connection(*args: object) -> None:
        stopped.set()
        raise connection_refused()

    client.read.side_effect = refuse_connection
    publish = mock.Mock()
    watch_endpoints(client, stopped, publish, interval=0)
    publish.assert_not_called()
    assert client.read.call_count == 1
    client.close.assert_called_once()


def test_menu_shortcuts_open_only_running_endpoints_and_stop_poller(tmp_path: Path) -> None:
    from f8platform.tray import run_tray

    backend = mock.MagicMock()
    icon = backend.Icon.return_value
    process = mock.Mock(spec=subprocess.Popen)
    process.stdin = mock.Mock()
    process.poll.return_value = None
    with (
        mock.patch.dict('sys.modules', {'pystray': backend}),
        mock.patch('f8platform.tray.sys.platform', 'darwin'),
        mock.patch('f8platform.tray.subprocess.Popen', return_value=process),
        mock.patch('f8platform.tray.PlatformClient'),
        mock.patch('f8platform.tray.Thread') as threads,
        mock.patch('f8platform.tray.webbrowser.open', return_value=True) as browser,
    ):
        def run_menu(setup) -> None:
            setup(icon)
            publish = threads.call_args_list[0].kwargs['args'][2]
            publish((TrayEndpoint('Open stopped', 'http://127.0.0.1:8222', False),
                     TrayEndpoint('Open running', 'http://127.0.0.1:8223', True)))
            menu_items = backend.MenuItem.call_args_list[-5:]
            assert menu_items[1].kwargs['enabled'] is False
            assert menu_items[2].kwargs['enabled'] is True
            menu_items[1].args[1]()
            browser.assert_not_called()
            menu_items[2].args[1]()
            browser.assert_called_once_with('http://127.0.0.1:8223')
            menu_items[-1].args[1]()

        icon.run.side_effect = run_menu
        run_tray(arguments=['--no-browser'], url='http://127.0.0.1:8209/bootstrap/secret', data_dir=tmp_path)
        assert threads.call_args_list[0].kwargs['args'][1].is_set()
        threads.return_value.join.assert_called_once_with(timeout=7)


def test_stuck_server_escalates_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    process = mock.Mock(spec=subprocess.Popen)
    process.pid = 123
    process.poll.return_value = None
    process.stdin = mock.Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired('studio', 15),
                                subprocess.TimeoutExpired('studio', 5), 0]
    stop_process(process)
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert 'did not stop' in caplog.text and 'killing PID' in caplog.text


def test_console_uses_argument_list_for_paths_with_spaces(tmp_path: Path) -> None:
    path = tmp_path / 'logs with spaces.txt'
    with mock.patch('f8platform.tray.os.name', 'posix'), \
         mock.patch('f8platform.tray.shutil.which', return_value='/usr/bin/xterm'), \
         mock.patch('f8platform.tray.subprocess.Popen') as popen:
        open_console(path)
    popen.assert_called_once_with(['/usr/bin/xterm', '-e', 'tail', '-n', '200', '-F', str(path)])


def test_menu_exit_leaves_process_cleanup_to_supervisor(tmp_path: Path) -> None:
    from f8platform.tray import run_tray

    backend = mock.MagicMock()
    icon = backend.Icon.return_value
    process = mock.Mock(spec=subprocess.Popen)
    process.stdin = mock.Mock()
    process.poll.return_value = None

    def exit_from_menu(_setup: object) -> None:
        callback = backend.MenuItem.call_args_list[-1].args[1]
        callback()
        icon.stop.assert_called_once()
        process.stdin.close.assert_not_called()

    icon.run.side_effect = exit_from_menu
    with (
        mock.patch.dict('sys.modules', {'pystray': backend}),
        mock.patch('f8platform.tray.sys.platform', 'darwin'),
        mock.patch('f8platform.tray.subprocess.Popen', return_value=process),
    ):
        run_tray(arguments=['--no-browser'], url='http://127.0.0.1:8210', data_dir=tmp_path)
    process.stdin.close.assert_called_once()
    process.wait.assert_called_once_with(timeout=15)
