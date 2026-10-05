"""Platform daemon, optional desktop tray and complete headless management CLI."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
from threading import Thread
import webbrowser

import msgspec
import uvicorn

from f8pysdk.platform_client import PlatformClient, PlatformConnection, segment
from .api import access_token, create_app


def _print(response: object) -> None:
    print(json.dumps(response, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description='Manage Feel8 components independently of WebStudio.')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('F8_PLATFORM_DATA_ROOT', str(Path.home() / '.feel8'))))
    parser.add_argument('--port', type=int)
    commands = parser.add_subparsers(dest='action', required=True)
    daemon = commands.add_parser('serve')
    daemon.add_argument('--start', action='append', default=[])
    daemon.add_argument('--distribution', type=Path)
    daemon.add_argument('--source-index', type=Path)
    daemon.add_argument('--development-config', type=Path)
    daemon.add_argument('--open-browser', action='store_true')
    daemon.add_argument('--tray', action='store_true')
    daemon.add_argument('--exit-on-stdin-close', action='store_true', help=argparse.SUPPRESS)
    commands.add_parser('open')
    commands.add_parser('list')
    raw = commands.add_parser('api', help='Call any authenticated platform endpoint')
    raw.add_argument('method', choices=('GET','POST','PUT','DELETE'))
    raw.add_argument('path')
    raw.add_argument('--body', type=Path, help='Request body JSON file')
    importer = commands.add_parser('import')
    importer.add_argument('location')
    importer.add_argument('--sha256', required=True)
    for action in ('prepare', 'select', 'update', 'uninstall'):
        command = commands.add_parser(action)
        command.add_argument('extension_id')
        command.add_argument('--sha256', required=True)
    for action in ('start', 'stop', 'deselect'):
        command = commands.add_parser(action)
        command.add_argument('extension_id')
    configuration = commands.add_parser('configure')
    configuration.add_argument('extension_id')
    configuration.add_argument('--endpoint', action='append', required=True, help='NAME=loopback HTTP URL')
    extensions = commands.add_parser('extensions').add_subparsers(dest='operation', required=True)
    jobs = commands.add_parser('jobs', help='Inspect queued maintenance tasks').add_subparsers(dest='operation', required=True)
    jobs.add_parser('list')
    for action in ('show', 'logs', 'cancel'):
        command = jobs.add_parser(action)
        command.add_argument('job_id')
    extensions.add_parser('list')
    extension_import = extensions.add_parser('import')
    extension_import.add_argument('url')
    extension_import.add_argument('--sha256', required=True)
    for action in ('detail','plan','install','cancel','enable','disable','uninstall'):
        command = extensions.add_parser(action)
        command.add_argument('extension_id')
    env = commands.add_parser('environments').add_subparsers(dest='operation', required=True)
    env.add_parser('list')
    env.add_parser('storage')
    env.add_parser('clean-unused')
    for action in ('detail','prepare','cancel','remove'):
        command = env.add_parser(action)
        command.add_argument('environment_id')
    tools = commands.add_parser('tools').add_subparsers(dest='operation', required=True)
    tools.add_parser('list')
    tools.add_parser('jobs')
    for action in ('job','cancel'):
        command = tools.add_parser(action)
        command.add_argument('job_id')
    run = tools.add_parser('run')
    run.add_argument('extension_id')
    run.add_argument('tool_id')
    run.add_argument('--arguments', type=Path, required=True, help='Tool arguments JSON file')
    run.add_argument('--confirm', action='store_true')
    services = commands.add_parser('services').add_subparsers(dest='operation', required=True)
    services.add_parser('list')
    logs = services.add_parser('logs')
    logs.add_argument('--after', type=int, default=0)
    start = services.add_parser('start')
    start.add_argument('service_id')
    start.add_argument('service_class')
    start.add_argument('--connect', action='append', default=[])
    stop = services.add_parser('stop')
    stop.add_argument('service_id')
    source = commands.add_parser('source').add_subparsers(dest='operation', required=True)
    source.add_parser('list')
    for action in ('start','stop'):
        command = source.add_parser(action)
        command.add_argument('extension_id')
    startup = commands.add_parser('startup')
    startup.add_argument('--application', action='append', default=None)
    args = parser.parse_args()
    if args.port is not None and not 0 < args.port < 65536:
        parser.error('Port must be between 1 and 65535')
    if args.action == 'serve':
        port = args.port or 8209
        url = f'http://127.0.0.1:{port}'
        token = access_token(args.data_dir)
        os.environ['F8_PLATFORM_URL'] = url
        os.environ['F8_PLATFORM_DATA_ROOT'] = str(args.data_dir.resolve())
        os.environ['F8_PLATFORM_CONNECTION_FILE'] = str((args.data_dir / 'platform.json').resolve())
        if args.tray:
            from .tray import run_tray
            forwarded = [argument for argument in sys.argv[1:] if argument != '--tray']
            run_tray(arguments=forwarded, url=f'{url}/bootstrap/{token}', data_dir=args.data_dir)
            return
        def shutdown() -> None:
            server.should_exit = True

        server = uvicorn.Server(uvicorn.Config(create_app(args.data_dir, startup=tuple(args.start),
            distribution=args.distribution, open_browser=args.open_browser, source_index=args.source_index,
            development=args.development_config, url=url, shutdown=shutdown), host='127.0.0.1', port=port, access_log=False))
        if args.exit_on_stdin_close:
            def watch_parent() -> None:
                try:
                    while os.read(sys.stdin.fileno(), 4096):
                        continue
                except OSError:
                    logging.getLogger(__name__).exception('Platform parent-watch pipe failed')
                server.should_exit = True
            Thread(target=watch_parent, name='platform-parent-watch', daemon=True).start()
        server.run()
        return
    connection_file = Path(os.environ.get('F8_PLATFORM_CONNECTION_FILE', str(args.data_dir / 'platform.json')))
    connection = (msgspec.json.decode(connection_file.read_bytes(), type=PlatformConnection) if connection_file.is_file()
                  else PlatformConnection(url=f'http://127.0.0.1:{args.port or 8209}', token_file=str(args.data_dir / 'platform-token')))
    if args.port is not None:
        connection = PlatformConnection(url=f'http://127.0.0.1:{args.port}', token_file=connection.token_file)
    if args.action == 'open':
        if not webbrowser.open(f'{connection.url}/bootstrap/{Path(connection.token_file).read_text().strip()}'):
            raise SystemExit('No browser available; use the headless management CLI.')
        return
    client = PlatformClient(connection)
    try:
        method, path, body = 'GET', '/api/applications', None
        if args.action == 'api':
            method, path = args.method, args.path
            body = json.loads(args.body.read_text()) if args.body else None
        elif args.action == 'import':
            method, path, body = 'POST', '/api/applications/import', {'location':args.location,'sha256':args.sha256}
        elif args.action == 'configure':
            method, path, body = 'POST', f'/api/applications/{segment(args.extension_id)}/configure', {'endpoints':dict(item.split('=',1) for item in args.endpoint)}
        elif args.action in {'prepare','select','update','uninstall'}:
            method, path, body = 'POST', f'/api/applications/{segment(args.extension_id)}/{args.action}', {'sha256':args.sha256}
        elif args.action in {'start','stop','deselect'}:
            method, path = 'POST', f'/api/applications/{segment(args.extension_id)}/{args.action}'
        elif args.action == 'jobs':
            path = '/api/management-jobs'
            if args.operation != 'list':
                path += '/' + segment(args.job_id)
                if args.operation in {'logs', 'cancel'}:
                    path += '/' + args.operation
                if args.operation == 'cancel':
                    method = 'POST'
        elif args.action == 'extensions':
            path = '/api/extensions'
            if args.operation == 'import':
                method, path, body = 'POST', path+'/import', {'url':args.url,'sha256':args.sha256}
            elif args.operation != 'list':
                path += '/'+segment(args.extension_id)
                if args.operation in {'enable','disable'}:
                    method, path, body = 'PUT', path+'/enabled', {'enabled':args.operation=='enable'}
                elif args.operation == 'uninstall':
                    method = 'DELETE'
                else:
                    path += '/'+args.operation
                    if args.operation in {'install','cancel'}:
                        method = 'POST'
        elif args.action == 'environments':
            path = '/api/environments'
            if args.operation == 'storage':
                path += '/storage'
            elif args.operation == 'clean-unused':
                method, path = 'POST', path+'/unused/clean'
            elif args.operation != 'list':
                path += '/'+segment(args.environment_id)
                if args.operation == 'remove':
                    method = 'DELETE'
                else:
                    path += '/'+args.operation
                    if args.operation != 'detail':
                        method = 'POST'
        elif args.action == 'tools':
            path = '/api/extension-tools' if args.operation == 'list' else '/api/tool-jobs'
            if args.operation == 'run':
                method, path, body = 'POST', f'/api/extension-tools/{segment(args.extension_id)}/{segment(args.tool_id)}/run', {'arguments':json.loads(args.arguments.read_text()),'confirm':args.confirm}
            elif args.operation in {'job','cancel'}:
                path += '/'+segment(args.job_id)
                if args.operation == 'cancel':
                    method, path = 'POST', path+'/cancel'
        elif args.action == 'services':
            path = '/api/service-processes'
            if args.operation == 'logs':
                path += f'/logs?after={args.after}'
            elif args.operation != 'list':
                method, path = 'POST', path+'/'+segment(args.service_id)+'/'+args.operation
                if args.operation == 'start':
                    body = {'serviceClass':args.service_class,'zenohConnect':args.connect}
        elif args.action == 'source':
            path = '/api/source-applications'
            if args.operation != 'list':
                method, path = 'POST', path+'/'+segment(args.extension_id)+'/'+args.operation
        elif args.action == 'startup':
            path = '/api/startup'
            if args.application is not None:
                method, body = 'PUT', {'applications':args.application}
        route, _, query = path.partition('?')
        response = client.request(method, route, content=msgspec.json.encode(body) if body is not None else None, params=query)
        response.raise_for_status()
        if response.content:
            _print(response.json())
    finally:
        client.close()


if __name__ == '__main__':
    main()
