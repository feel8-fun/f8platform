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
from threading import Event, Thread
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
        process = subprocess.Popen(
            [sys.executable, '-u', '-m', 'f8platform', *arguments, '--exit-on-stdin-close'],
            stdin=subprocess.PIPE, stdout=output, stderr=subprocess.STDOUT,
            start_new_session=os.name != 'nt',
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
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
            icon.stop()

        def shortcut(entry: TrayEndpoint) -> pystray.MenuItem:
            def open_endpoint() -> None:
                if entry.running:
                    open_page(entry.url)
            return pystray.MenuItem(entry.label, open_endpoint, enabled=entry.running)

        def update_shortcuts(entries: tuple[TrayEndpoint, ...]) -> None:
            icon.menu = pystray.Menu(
                pystray.MenuItem('Manage extensions', open_platform, default=True),
                *(shortcut(entry) for entry in entries),
                pystray.MenuItem('Open console / logs', show_console),
                pystray.MenuItem('Exit', exit_platform),
            )
            icon.update_menu()

        update_shortcuts(())
        stopped = Event()
        endpoint_thread: Thread | None = None

        def monitor() -> None:
            returncode = process.wait()
            stopped.set()
            if returncode:
                logger.error('Platform exited with code %s; log: %s', returncode, log_path)
                show_console()
            icon.stop()

        def setup(_tray_icon: object) -> None:
            nonlocal endpoint_thread
            icon.visible = True
            address = urlsplit(url)
            client = PlatformClient(PlatformConnection(url=f'{address.scheme}://{address.netloc}',
                token_file=str(data_dir / 'platform-token')))
            client.http.timeout = httpx.Timeout(3.0)
            endpoint_thread = Thread(target=watch_endpoints, args=(client, stopped, update_shortcuts),
                                     name='platform-tray-endpoints', daemon=True)
            endpoint_thread.start()
            Thread(target=monitor, name='platform-tray-monitor', daemon=True).start()

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
            if endpoint_thread is not None:
                endpoint_thread.join(timeout=7)
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
            stop_process(process)
            icon.stop()
