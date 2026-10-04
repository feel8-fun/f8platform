"""Cross-process ownership of one writable platform installation."""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
import errno
import os
from pathlib import Path
from typing import BinaryIO

from .errors import ConflictError


def _lock(handle: BinaryIO) -> None:
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def single_platform_instance(data_dir: Path) -> Generator[None]:
    path = data_dir / 'platform.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        try:
            _lock(handle)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            raise ConflictError('A platform manager already owns this data directory') from exc
        try:
            yield
        finally:
            _unlock(handle)
