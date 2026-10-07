"""Read package metadata and account for file blocks without counting hardlinks twice."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from importlib.metadata import distributions
import os
import logging
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

import msgspec

from f8pysdk.extension_status import EnvironmentPackage, EnvironmentUsage
from f8pysdk.specs import F8JsonValue
from .environment_definitions import object_value, selected_lock


def file_stats(roots: Iterable[Path]) -> Iterator[tuple[Path, os.stat_result]]:
    for root in roots:
        if root.is_symlink():
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if entry.is_symlink():
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            else:
                                # Windows DirEntry.stat omits file identity and
                                # link counts; os.stat supplies them for NTFS.
                                result = (os.stat(entry.path, follow_symlinks=False) if os.name == 'nt'
                                          else entry.stat(follow_symlinks=False))
                                yield directory, result
                        except FileNotFoundError:
                            logging.getLogger(__name__).debug(
                                'File disappeared during storage inspection: %s', entry.path, exc_info=True
                            )
            except FileNotFoundError:
                logging.getLogger(__name__).debug(
                    'Entry disappeared during storage inspection: %s', directory, exc_info=True
                )


@dataclass
class UsageCounter:
    logical: int = 0
    unique: int = 0
    shared: int = 0
    exclusive: int = 0
    allocated: int = 0
    exclusive_allocated: int = 0
    seen: set[tuple[int, int]] = field(default_factory=set)

    def add(self, stat: os.stat_result) -> None:
        self.logical += stat.st_size
        identity = (stat.st_dev, stat.st_ino)
        if identity in self.seen:
            return
        self.seen.add(identity)
        self.unique += stat.st_size
        blocks = stat.st_blocks * 512 if os.name != 'nt' else 0
        self.allocated += blocks
        if stat.st_nlink > 1:
            self.shared += stat.st_size
        else:
            self.exclusive += stat.st_size
            self.exclusive_allocated += blocks

    def result(self) -> EnvironmentUsage:
        return EnvironmentUsage(logical_bytes=self.logical, unique_file_bytes=self.unique,
            shared_link_bytes=self.shared, exclusive_file_bytes=self.exclusive,
            allocated_bytes=self.allocated if os.name != 'nt' else None,
            exclusive_allocated_bytes=self.exclusive_allocated if os.name != 'nt' else None)


def paths_usage(roots: Iterable[Path]) -> EnvironmentUsage:
    counter = UsageCounter()
    for _directory, stat in file_stats(roots):
        counter.add(stat)
    return counter.result()


@dataclass(frozen=True)
class StorageUsage:
    environments: EnvironmentUsage
    cache: EnvironmentUsage
    total: EnvironmentUsage
    unused: dict[Path, EnvironmentUsage]


def storage_usage(roots: tuple[Path, ...], cache: Path, unused: tuple[Path, ...]) -> StorageUsage:
    """Scan each tree once and accumulate separate and combined hardlink totals."""
    environments, cached, total = UsageCounter(), UsageCounter(), UsageCounter()
    unused_counters = {path: UsageCounter() for path in unused}
    combined = set((*roots, cache))
    scan_roots = tuple(path for path in combined if not any(path != other and path.is_relative_to(other) for other in combined))
    for root in scan_roots:
        root_is_environment = any(root.is_relative_to(path) for path in roots)
        root_is_cache = root.is_relative_to(cache)
        nested_environments = () if root_is_environment else tuple(path for path in roots if path.is_relative_to(root))
        nested_cache = not root_is_cache and cache.is_relative_to(root)
        nested_unused = {path: counter for path, counter in unused_counters.items() if path.is_relative_to(root)}
        previous: Path | None = None
        counters: tuple[UsageCounter, ...] = ()
        for directory, stat in file_stats((root,)):
            if directory != previous:
                in_environment = root_is_environment or any(directory.is_relative_to(path) for path in nested_environments)
                in_cache = root_is_cache or (nested_cache and directory.is_relative_to(cache))
                counters = (total, *((environments,) if in_environment else ()), *((cached,) if in_cache else ()),
                            *(counter for path, counter in nested_unused.items() if directory.is_relative_to(path)))
                previous = directory
            for counter in counters:
                counter.add(stat)
    return StorageUsage(environments=environments.result(), cache=cached.result(), total=total.result(),
                        unused={path: counter.result() for path, counter in unused_counters.items()})


def directory_usage(root: Path) -> EnvironmentUsage:
    return paths_usage((root,))


def environment_packages(
    root: Path, prefix: Path, environment: str
) -> tuple[Literal["installed", "locked"], tuple[EnvironmentPackage, ...]]:
    packages: list[EnvironmentPackage] = []
    metadata = prefix / "conda-meta"
    if metadata.is_dir():
        for record in sorted(metadata.glob("*.json")):
            data = msgspec.json.decode(record.read_bytes(), type=dict[str, F8JsonValue])
            if not isinstance(data.get("name"), str) or not isinstance(data.get("version"), str):
                raise ValueError(f"Invalid installed package metadata: {record}")
            packages.append(
                EnvironmentPackage(
                    name=str(data["name"]),
                    version=str(data["version"]),
                    manager="conda",
                    build=str(data.get("build", "")),
                    platform=str(data.get("subdir", "")),
                )
            )
        site_packages = [prefix / "Lib/site-packages", *prefix.glob("lib/python*/site-packages")]
        for distribution in distributions(path=[str(path) for path in site_packages if path.is_dir()]):
            name = distribution.metadata["Name"]
            if not name:
                raise ValueError(f"Installed Python distribution is missing its name in {prefix}")
            packages.append(EnvironmentPackage(name=name, version=distribution.version, manager="pypi"))
        return "installed", tuple(sorted(packages, key=lambda package: (package.manager, package.name)))
    lock = selected_lock(root, environment)
    if lock is None:
        return "locked", ()
    entries = lock.get("packages", [])
    if not isinstance(entries, list):
        raise ValueError("Invalid locked package metadata")
    for value in entries:
        entry = object_value(value, "Locked package metadata")
        conda = entry.get("conda")
        pypi = entry.get("pypi")
        if isinstance(conda, str):
            url = urlparse(conda)
            filename = Path(unquote(url.path)).name.removesuffix(".conda").removesuffix(".tar.bz2")
            name, version, build = filename.rsplit("-", 2)
            packages.append(
                EnvironmentPackage(
                    name=name, version=version, build=build, manager="conda", platform=Path(url.path).parent.name
                )
            )
        elif isinstance(pypi, str):
            packages.append(
                EnvironmentPackage(
                    name=str(entry.get("name", Path(unquote(urlparse(pypi).path)).name)),
                    version=str(entry.get("version", "unknown")),
                    manager="pypi",
                )
            )
    return "locked", tuple(sorted(packages, key=lambda package: (package.manager, package.name, package.platform)))
