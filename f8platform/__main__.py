"""Platform daemon and CLI; neither requires a Studio installation."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
import uvicorn

from .api import access_token, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description='Manage Feel8 components independently of WebStudio.')
    parser.add_argument('--data-dir', type=Path, default=Path(os.environ.get('F8_PLATFORM_DATA_ROOT', str(Path.home() / '.feel8'))))
    parser.add_argument('--port', type=int, default=8209)
    commands = parser.add_subparsers(dest='action', required=True)
    daemon = commands.add_parser('serve')
    daemon.add_argument('--start', action='append', default=[])
    daemon.add_argument('--distribution', type=Path)
    daemon.add_argument('--open-browser', action='store_true')
    commands.add_parser('list')
    importer = commands.add_parser('import')
    importer.add_argument('location')
    importer.add_argument('--sha256', required=True)
    for action in ('prepare', 'select', 'update', 'uninstall'):
        command = commands.add_parser(action)
        command.add_argument('component_id')
        command.add_argument('--sha256', required=True)
    for action in ('start', 'stop'):
        command = commands.add_parser(action)
        command.add_argument('component_id')
    configuration = commands.add_parser('configure')
    configuration.add_argument('component_id')
    configuration.add_argument('--endpoint', action='append', required=True, help='NAME=loopback HTTP URL')
    args = parser.parse_args()
    if not 0 < args.port < 65536:
        parser.error('Port must be between 1 and 65535')
    if args.action == 'serve':
        uvicorn.run(create_app(args.data_dir, startup=tuple(args.start), distribution=args.distribution,
                               open_browser=args.open_browser), host='127.0.0.1', port=args.port)
        return
    token = access_token(args.data_dir)
    with httpx.Client(base_url=f'http://127.0.0.1:{args.port}', headers={'Authorization': f'Bearer {token}'},
                      timeout=None, trust_env=False) as client:
        if args.action == 'list':
            response = client.get('/api/components')
        elif args.action == 'import':
            response = client.post('/api/components/import', json={'location': args.location, 'sha256': args.sha256})
        elif args.action == 'configure':
            endpoints = dict(item.split('=', 1) for item in args.endpoint)
            response = client.post(f'/api/components/{args.component_id}/configure', json={'endpoints': endpoints})
        elif args.action in {'prepare', 'select', 'update', 'uninstall'}:
            response = client.post(f'/api/components/{args.component_id}/{args.action}', json={'sha256': args.sha256})
        else:
            response = client.post(f'/api/components/{args.component_id}/{args.action}')
        response.raise_for_status()
        if response.content:
            print(json.dumps(response.json(), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
