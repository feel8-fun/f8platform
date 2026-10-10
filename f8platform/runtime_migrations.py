"""Compatibility with historical runtime storage identities."""
from __future__ import annotations
import hashlib
import tomllib
from pathlib import Path
from .environment_definitions import environment_identity

def legacy_environment_id(environment: str, *, official_source_root: Path, development_root: Path | None) -> str | None:
    bundled = environment == 'studio-runtime' and (official_source_root / 'env').is_dir()
    root = official_source_root if bundled else development_root
    if root is None or not (root / 'pixi.lock').is_file():
        return None
    definitions = tomllib.loads((root / 'pixi.toml').read_text(encoding='utf-8')).get('environments', {})
    if environment not in definitions:
        return None
    identity = environment_identity(root, environment)
    if bundled:
        return f'bundled-base-{identity}'
    location = hashlib.sha256(str(root).encode()).hexdigest()[:16]
    return f'workspace-{location}-{environment}-{identity[:16]}'
