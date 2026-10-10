"""Desktop tray supervisor; closing the browser does not stop Platform."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from importlib.resources import files
import shutil
import signal
import subprocess
import sys
from threading import Event, Lock, Thread
from urllib.parse import urlsplit
import webbrowser

import httpx
from f8pysdk.platform_client import PlatformClient, PlatformConnection

from .tray_endpoints import TrayEndpoint, watch_endpoints

logger = logging.getLogger(__name__)


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        logger.warning("Platform did not stop within 15 seconds; terminating PID %s", process.pid)
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("Platform did not terminate; killing PID %s", process.pid)
            process.kill()
            process.wait()


def open_console(path: Path) -> None:
    if os.name == 'nt':
        subprocess.Popen(['powershell.exe', '-NoExit', '-NoProfile', '-Command',
                          'Get-Content -LiteralPath $env:F8_PLATFORM_LOG -Tail 200 -Wait'],
                         env={**os.environ, 'F8_PLATFORM_LOG': str(path)},
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        terminal = shutil.which('x-terminal-emulator') or shutil.which('xterm')
        if terminal:
            subprocess.Popen([terminal, '-e', 'tail', '-n', '200', '-F', str(path)])
        elif not webbrowser.open(path.as_uri()):
            raise OSError(f'No terminal or file viewer available; log: {path}')


def run_tray(*, arguments: list[str], url: str, data_dir: Path) -> None:
    # Import only for desktop mode. Linux needs GTK/AppIndicator, not the Xorg
    # backend (which cannot display a context menu).
    if sys.platform.startswith('linux'):
        os.environ.setdefault('PYSTRAY_BACKEND', 'gtk')
    try:
        import pystray
        from PIL import Image
        with files('f8platform').joinpath('portal_assets', 'tray-icon.png').open('rb') as source:
            icon_image = Image.open(source).convert('RGBA')
        icon = pystray.Icon('f8platform', icon_image, 'Feel8 Platform')
        if not icon.HAS_MENU:
            raise RuntimeError('This desktop tray backend does not support menus')
    except (ImportError, RuntimeError, OSError):
        logger.warning('Tray unavailable; continuing in console mode', exc_info=True)
        subprocess.run([sys.executable, '-m', 'f8platform', *arguments], check=True)
        return
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / 'platform-console.log'
    with log_path.open('ab', buffering=0) as output:
        def launch_process() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                [sys.executable, '-u', '-m', 'f8platform', *arguments, '--exit-on-stdin-close'],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )

        process = launch_process()
        stopped = Event()
        restarting = Event()
        restart_lock = Lock()
        menu_lock = Lock()
        endpoints_stopped = Event()
        endpoint_thread: Thread | None = None
        monitor_thread: Thread | None = None
        restart_thread: Thread | None = None
        current_entries: tuple[TrayEndpoint, ...] = ()
        def open_page(address: str) -> None:
            try:
                if not webbrowser.open(address):
                    logger.warning('No browser available to open the requested page')
            except (OSError, webbrowser.Error):
                logger.exception('Failed to open browser')

        def open_platform() -> None:
            open_page(url)

        def show_console() -> None:
            try:
                open_console(log_path)
            except (OSError, webbrowser.Error):
                logger.exception('Failed to open Platform console: %s', log_path)

        def exit_platform() -> None:
            # Keep the UI callback nonblocking; the supervisor's finally owns cleanup.
            stopped.set()
            icon.stop()

        def shortcut(entry: TrayEndpoint) -> pystray.MenuItem:
            def open_endpoint() -> None:
                if entry.running:
                    open_page(entry.url)
            return pystray.MenuItem(entry.label, open_endpoint, enabled=entry.running)

        def update_shortcuts(entries: tuple[TrayEndpoint, ...] | None) -> None:
            nonlocal current_entries
            with menu_lock:
                if entries is not None:
                    current_entries = entries
                endpoints = pystray.Menu(*(shortcut(entry) for entry in current_entries)) if current_entries else pystray.Menu(
                    pystray.MenuItem('No application endpoints', lambda: None, enabled=False),
                )
                icon.menu = pystray.Menu(
                    pystray.MenuItem('Manage extensions', open_platform, default=True, enabled=not restarting.is_set()),
                    pystray.MenuItem('Endpoints', endpoints, enabled=bool(current_entries) and not restarting.is_set()),
                    pystray.MenuItem('Restarting Platform…' if restarting.is_set() else 'Restart Platform',
                                     request_restart, enabled=not restarting.is_set()),
                    pystray.MenuItem('Open console / logs', show_console),
                    pystray.MenuItem('Exit', exit_platform),
                )
                icon.update_menu()

        def monitor(monitored_process: subprocess.Popen[bytes]) -> None:
            returncode = monitored_process.wait()
            if stopped.is_set() or restarting.is_set() or monitored_process is not process:
                return
            stopped.set()
            if returncode:
                logger.error('Platform exited with code %s; log: %s', returncode, log_path)
                show_console()
            icon.stop()

        def start_watchers() -> None:
            nonlocal endpoint_thread, endpoints_stopped, monitor_thread
            monitor_thread = Thread(target=monitor, args=(process,), name='platform-tray-monitor', daemon=True)
            monitor_thread.start()
            endpoints_stopped = Event()
            address = urlsplit(url)
            client = PlatformClient(PlatformConnection(url=f'{address.scheme}://{address.netloc}',
                token_file=str(data_dir / 'platform-token')))
            client.http.timeout = httpx.Timeout(3.0)
            endpoint_thread = Thread(target=watch_endpoints, args=(client, endpoints_stopped, update_shortcuts),
                                     name='platform-tray-endpoints', daemon=True)
            endpoint_thread.start()

        def restart_platform() -> None:
            nonlocal process
            failed = False
            try:
                endpoints_stopped.set()
                if endpoint_thread is not None:
                    endpoint_thread.join(timeout=7)
                update_shortcuts(())
                stop_process(process)
                if monitor_thread is not None:
                    monitor_thread.join()
                if stopped.is_set():
                    return
                logger.info('Restarting Platform')
                process = launch_process()
                if not stopped.is_set():
                    start_watchers()
                logger.info('Platform replacement started with PID %s', process.pid)
            except Exception:
                # Restart runs at a background-thread boundary; keep the tray usable for retry.
                failed = True
                logger.exception('Failed to restart Platform; console log: %s', log_path)
                show_console()
            finally:
                restarting.clear()
                if not stopped.is_set():
                    update_shortcuts(None)
                    if not failed and process.poll() is not None:
                        monitor(process)

        def request_restart() -> None:
            nonlocal restart_thread
            with restart_lock:
                if stopped.is_set() or restarting.is_set():
                    return
                restarting.set()
                update_shortcuts(None)
                restart_thread = Thread(target=restart_platform, name='platform-tray-restart', daemon=True)
                restart_thread.start()

        def setup(_tray_icon: object) -> None:
            icon.visible = True
            start_watchers()

        update_shortcuts(())

        print(f'Platform tray is running. Console log: {log_path}', flush=True)
        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.signal(signal.SIGTERM, lambda signum, frame: icon.stop())
        if sys.platform.startswith('linux'):
            from gi.repository import GLib  # pyright: ignore[reportMissingImports, reportAttributeAccessIssue, reportUnknownVariableType]  # Linux-only GI exposes native modules without stubs.

            def install_interrupt_handler() -> bool:
                # GTK's pystray backend resets SIGINT during initialization.
                # Run on the main loop after that reset, before user interaction.
                signal.signal(signal.SIGINT, lambda signum, frame: icon.stop())
                return False

            GLib.idle_add(install_interrupt_handler)  # pyright: ignore[reportUnknownMemberType]
        try:
            icon.run(setup)  # pyright: ignore[reportUnknownMemberType]  # pystray leaves setup untyped
        except KeyboardInterrupt:
            logger.info('Stopping Platform tray')
        finally:
            stopped.set()
            endpoints_stopped.set()
            if restart_thread is not None:
                restart_thread.join()
            # A restart may have created its new poller while shutdown was requested.
            endpoints_stopped.set()
            if endpoint_thread is not None:
                endpoint_thread.join(timeout=7)
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
            stop_process(process)
            icon.stop()
