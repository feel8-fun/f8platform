"""Build and publish the platform bootstrap independently of applications."""
from pathlib import Path
import hashlib
import subprocess
import sys
import tomllib
from typing import Literal

from f8pysdk.runtime_packaging import build_runtime


def main() -> None:
    root = Path(__file__).resolve().parent
    version = tomllib.loads((root / 'pyproject.toml').read_text())['project']['version']
    wheels = root / 'build/wheels'
    wheels.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                    '-w', str(wheels), str(root)], check=True)
    candidates = list(wheels.glob(f'f8platform-{version}-*.whl'))
    if len(candidates) != 1:
        raise ValueError('Expected one platform wheel for this version')
    platform: Literal['linux-x86_64', 'windows-x86_64'] = 'windows-x86_64' if sys.platform == 'win32' else 'linux-x86_64'
    output = build_runtime(root, candidates[0], root / f'dist/f8platform-{version}-{platform}.zip',
                          runtime_id='platform-runtime', provider_id='feel8.platform', version=version, platform=platform)
    output.with_suffix('.zip.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest() + '  ' + output.name + '\n')
    print(output)


if __name__ == '__main__':
    main()
