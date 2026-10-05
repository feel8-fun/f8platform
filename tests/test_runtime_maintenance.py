from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest
import yaml

from f8platform.environment_definitions import environment_identity
from f8platform.environments import EnvironmentManager
from f8platform.errors import ConflictError
from f8platform.runtime_registry import RuntimeRegistry, directory_usage
from f8platform.runtime_inspection import paths_usage, storage_usage


def source(root: Path) -> EnvironmentManager:
    root.mkdir(parents=True)
    (root / 'pixi.toml').write_text('[workspace]\nname="base"\nchannels=["conda-forge"]\nplatforms=["linux-64"]\n'
        '[feature.base.dependencies]\npython="3.12.*"\nnumpy=">=1.26,<3"\n'
        '[feature.other.dependencies]\npython="3.12.*"\n'
        '[environments]\nbase={features=["base"],no-default-feature=true}\nother={features=["other"],no-default-feature=true}\n')
    write_lock(root, 'base', numpy='1.26.4', other=True)
    return EnvironmentManager(root.parent / 'data', root)



def write_lock(root: Path, environment: str, *, numpy: str = '1.26.4', other: bool = False) -> None:
    python = 'https://conda.anaconda.org/conda-forge/linux-64/python-3.12.9-build_0.conda'
    array = f'https://conda.anaconda.org/conda-forge/linux-64/numpy-{numpy}-build_0.conda'
    envs = {environment: {'channels': [{'url': 'https://conda.anaconda.org/conda-forge/'}],
                          'packages': {'linux-64': [{'conda': python}, {'conda': array}]}}}
    if other:
        envs['other'] = {'packages': {'linux-64': [{'conda': python}]}}
    (root / 'pixi.lock').write_text(yaml.safe_dump({'version': 6, 'environments': envs, 'packages': [
        {'conda': python, 'sha256': 'a' * 64}, {'conda': array, 'sha256': 'b' * 64},
    ]}))



def test_environment_identity_ignores_unrelated_features_and_locked_packages(tmp_path: Path) -> None:
    manager = source(tmp_path / 'base')
    initial = environment_identity(manager.source_root, 'base')
    manifest = manager.source_root / 'pixi.toml'
    manifest.write_text(manifest.read_text().replace('[feature.other.dependencies]\npython="3.12.*"', '[feature.other.dependencies]\npython="3.13.*"'))
    lock = yaml.safe_load((manager.source_root / 'pixi.lock').read_text())
    lock['environments']['other']['packages']['linux-64'].append({'conda': 'other-package'})
    lock['packages'].append({'conda': 'other-package', 'sha256': 'new'})
    (manager.source_root / 'pixi.lock').write_text(yaml.safe_dump(lock))
    assert environment_identity(manager.source_root, 'base') == initial
    write_lock(manager.source_root, 'base', numpy='2.0.0', other=True)
    assert environment_identity(manager.source_root, 'base') != initial



def test_hardlink_usage_does_not_sum_shared_files_as_exclusive(tmp_path: Path) -> None:
    root = tmp_path / 'env'
    root.mkdir()
    cached = tmp_path / 'cached'
    cached.write_bytes(b'x' * 128)
    os.link(cached, root / 'package')
    os.link(cached, root / 'second-name')
    (root / 'private').write_bytes(b'x' * 32)
    usage = directory_usage(root)
    assert usage.logical_bytes == 288
    assert usage.unique_file_bytes == 160
    assert usage.shared_link_bytes == 128
    assert usage.exclusive_file_bytes == 32



def test_environment_detail_reads_installed_or_locked_packages(tmp_path: Path) -> None:
    manager = source(tmp_path / 'extension')
    registry = RuntimeRegistry(tmp_path / 'data', manager)
    registry.add_preset('base')
    identifier = next(iter(registry.sources))
    locked = registry.detail(identifier)
    assert locked.package_inventory == 'locked'
    assert {package.name for package in locked.packages} == {'numpy', 'python'}
    prefix = Path(locked.storage_path)
    (prefix / 'conda-meta').mkdir(parents=True)
    (prefix / 'conda-meta/python.json').write_text(json.dumps({
        'name': 'python', 'version': '3.12.9', 'build': 'build_0', 'subdir': 'linux-64',
    }))
    dist = prefix / 'lib/python3.12/site-packages/requests-2.32.0.dist-info'
    dist.mkdir(parents=True)
    (dist / 'METADATA').write_text('Metadata-Version: 2.1\nName: requests\nVersion: 2.32.0\n')
    installed = registry.detail(identifier)
    assert installed.package_inventory == 'installed'
    assert [(package.manager, package.name, package.version) for package in installed.packages] == [
        ('conda', 'python', '3.12.9'), ('pypi', 'requests', '2.32.0'),
    ]


def test_total_storage_deduplicates_hardlinks_and_reports_file_blocks(tmp_path: Path) -> None:
    first = tmp_path / 'env'
    second = tmp_path / 'cache'
    first.mkdir()
    second.mkdir()
    cached = second / 'package'
    cached.write_bytes(b'x' * 128)
    os.link(cached, first / 'installed')
    (first / 'private').write_bytes(b'y' * 32)
    total = paths_usage((first, second))
    assert total.unique_file_bytes == 160
    assert total.logical_bytes == 288
    assert total.shared_link_bytes == 128
    if os.name != 'nt':
        assert total.allocated_bytes == cached.stat().st_blocks * 512 + (first / 'private').stat().st_blocks * 512
        assert total.exclusive_allocated_bytes == (first / 'private').stat().st_blocks * 512


def test_storage_groups_keep_hardlink_totals_and_unused_sizes_consistent(tmp_path: Path) -> None:
    environments, cache = tmp_path / 'envs', tmp_path / 'cache'
    unused = environments / 'old'
    unused.mkdir(parents=True)
    cache.mkdir()
    package = cache / 'package'
    package.write_bytes(b'x' * 128)
    os.link(package, unused / 'shared')
    (unused / 'private').write_bytes(b'y' * 32)
    result = storage_usage((environments,), cache, (unused,))
    assert result.environments.unique_file_bytes == 160
    assert result.cache.unique_file_bytes == 128
    assert result.total.unique_file_bytes == 160
    assert result.total.logical_bytes == 288
    assert result.unused[unused] == directory_usage(unused)


def test_storage_snapshot_is_reused_but_explicit_refresh_and_invalidation_remeasure(tmp_path: Path) -> None:
    registry = RuntimeRegistry(tmp_path / 'data', source(tmp_path / 'extension'))
    cache = registry.storage / 'package-cache'
    cache.mkdir(parents=True)
    package = cache / 'package'
    package.write_bytes(b'x' * 32)
    first = registry.storage_status()
    package.write_bytes(b'x' * 64)
    assert registry.storage_status().cache_usage.unique_file_bytes == 32
    assert registry.storage_status().usage_updated_at == first.usage_updated_at
    refreshed = registry.storage_status(refresh=True)
    assert refreshed.cache_usage.unique_file_bytes == 64
    with registry.maintenance():
        assert not registry.storage_status().can_change
    package.write_bytes(b'x' * 128)
    assert registry.storage_status().cache_usage.unique_file_bytes == 128


def test_storage_scan_does_not_block_maintenance_and_invalidated_results_are_remeasured(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    registry = RuntimeRegistry(tmp_path / 'data', source(tmp_path / 'extension'))
    entered, release = Event(), Event()

    def scan(roots: tuple[Path, ...], cache: Path, unused: tuple[Path, ...]):
        entered.set()
        assert release.wait(5)
        return storage_usage(roots, cache, unused)

    with patch('f8platform.runtime_registry.storage_usage', side_effect=scan) as scanning, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(registry.storage_status)
        try:
            assert entered.wait(5)
            second = executor.submit(registry.storage_status)
            # Read-only scans must not hold the runtime lifecycle lock.
            with registry.maintenance():
                assert registry.busy
        finally:
            release.set()
        # Maintenance invalidates the running snapshot; the next read must remeasure.
        first.result(timeout=5)
        second.result(timeout=5)
        assert scanning.call_count == 2


def test_concurrent_storage_reads_share_one_measurement(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    registry = RuntimeRegistry(tmp_path / 'data', source(tmp_path / 'extension'))
    entered, release = Event(), Event()

    def scan(roots: tuple[Path, ...], cache: Path, unused: tuple[Path, ...]):
        entered.set()
        assert release.wait(5)
        return storage_usage(roots, cache, unused)

    with patch('f8platform.runtime_registry.storage_usage', side_effect=scan) as scanning, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(registry.storage_status)
        try:
            assert entered.wait(5)
            second = executor.submit(registry.storage_status)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
        assert scanning.call_count == 1


def test_runtime_maintenance_blocks_preparation(tmp_path: Path) -> None:
    registry = RuntimeRegistry(tmp_path / 'data', source(tmp_path / 'extension'))
    registry.add_preset('base')
    identifier = next(iter(registry.sources))
    with registry.maintenance():
        with pytest.raises(ConflictError, match='maintenance'):
            asyncio.run(registry.prepare(identifier))
    assert not registry.busy


def test_unused_cleanup_preserves_referenced_unknown_and_external_directories(tmp_path: Path) -> None:
    manager = source(tmp_path / 'extension')
    registry = RuntimeRegistry(tmp_path / 'data', manager)
    root = registry.storage / 'runtimes'
    used = f"pixi-{'1' * 16}-base-{'a' * 64}"
    old = f"pixi-{'2' * 16}-base-{'b' * 64}"
    retired = f"user-{'c' * 64}"
    unknown = ('important-user-data', 'pixi-personal-data')
    for name in (used, old, retired, *unknown):
        path = root / name
        path.mkdir(parents=True)
        (path / 'pixi.toml').write_text('definition')
        (path / 'private').write_text('data')
    unused = registry.unused_environments({used})
    assert {item.environment_id for item in unused} == {old, retired}
    cache = registry.storage / 'package-cache'
    cache.mkdir()
    cached = cache / 'package'
    cached.write_text('shared package')
    os.link(cached, root / old / 'shared-package')
    with registry.maintenance():
        registry.clean_unused_environments({used})
    assert cached.read_text() == 'shared package'
    assert (root / used / 'private').read_text() == 'data'
    for name in unknown:
        assert (root / name / 'private').read_text() == 'data'
    assert not (root / old).exists()
    assert not (root / retired).exists()
    assert manager.source_root.is_dir()


@pytest.mark.parametrize('symlink_root', [False, True])
def test_unused_cleanup_preserves_symlinked_directories(tmp_path: Path, symlink_root: bool) -> None:
    registry = RuntimeRegistry(tmp_path / 'data', source(tmp_path / 'extension'))
    root = registry.storage / 'runtimes'
    identifier = f"pixi-{'1' * 16}-base-{'a' * 64}"
    external = tmp_path / 'external'
    external.mkdir()
    target = external / identifier if symlink_root else external
    target.mkdir(exist_ok=True)
    (target / 'pixi.toml').write_text('definition')
    (target / 'private').write_text('keep')
    try:
        if symlink_root:
            root.parent.mkdir(parents=True, exist_ok=True)
            root.symlink_to(external, target_is_directory=True)
        else:
            root.mkdir(parents=True)
            (root / identifier).symlink_to(external, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f'Symlink creation is unavailable: {exc}')
    assert registry.unused_environments(set()) == ()
    with registry.maintenance():
        registry.clean_unused_environments(set())
    assert (target / 'private').read_text() == 'keep'


def test_cleanup_failure_does_not_leave_maintenance_locked(tmp_path: Path) -> None:
    registry = RuntimeRegistry(tmp_path / 'data', source(tmp_path / 'extension'))
    directory = registry.storage / 'runtimes' / f"pixi-{'1' * 16}-base-{'a' * 64}"
    directory.mkdir(parents=True)
    (directory / 'pixi.toml').write_text('definition')
    with patch.object(shutil, 'rmtree', side_effect=OSError('environment file is locked')):
        with pytest.raises(OSError, match='environment file is locked'):
            with registry.maintenance():
                registry.clean_unused_environments(set())
    assert not registry.busy


def test_sparse_file_reports_allocated_blocks_separately(tmp_path: Path) -> None:
    if os.name == 'nt':
        pytest.skip('Allocated Unix file blocks are unavailable on Windows')
    sparse = tmp_path / 'sparse'
    with sparse.open('wb') as stream:
        stream.seek(1024 * 1024)
        stream.write(b'x')
    usage = directory_usage(tmp_path)
    assert usage.unique_file_bytes == 1024 * 1024 + 1
    assert usage.allocated_bytes == sparse.stat().st_blocks * 512
