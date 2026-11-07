"""Select the current skill source, independently of local research directories."""
from __future__ import annotations

import json
import os
from pathlib import Path


ROOT_FILES = {
    'README.md', 'SKILL.md', 'pyproject.toml', 'requirements.txt',
    'requirements-kronos.txt', 'requirements-kronos.lock.txt',
}
SOURCE_DIRS = {
    'scripts': {'.py'}, 'tests': {'.py'},
    'references': {'.md'}, 'policies': {'.json'},
}
EXAMPLE_FILES = {
    'actions-template.csv', 'financial-facts-template.csv', 'news-template.json',
    'prices-template.csv', 'request-hk.json', 'request-us.json',
    'study-protocol.json', 'multifactor-protocol.json',
}
LOCAL_DIRS = {
    '.git', '.venv', '__pycache__', '.pytest_cache', '.pytest-tmp', '.ruff_cache',
    '.install-tmp', '.pip-cache', 'docs', 'runtime', 'reports', 'cache', 'node_modules',
    'model-comparison', 'hourly-model-test', 'full-hourly-model-test',
    'input-parity', 'walk-forward-baseline',
}
ASSET_MANIFEST = 'model-assets/kronos-small/manifest.json'


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError(f'unsafe package path: {relative}')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'package path leaves source: {relative}')
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
        raise ValueError(f'package symlink not accepted: {relative}')
    return path


def package_files(root: Path, *, include_weights: bool = True, manifest: dict | None = None) -> list[Path]:
    """Only declared first-party files and pinned upstream assets enter a package.

    Checkpoints may be absent in a source clone; the installer retrieves them
    using the fixed manifest. Local output never expands this selection.
    """
    root = root.resolve()
    selected = set(ROOT_FILES)
    selected.update('examples/' + name for name in EXAMPLE_FILES)
    for directory, suffixes in SOURCE_DIRS.items():
        source = _file(root, directory)
        if not source.exists():
            continue
        for current, dirs, files in os.walk(source, followlinks=False):
            here = Path(current)
            dirs[:] = sorted(name for name in dirs if name not in LOCAL_DIRS)
            for name in dirs:
                _file(root, (here / name).relative_to(root).as_posix())
            for name in files:
                path = here / name
                if path.suffix in suffixes and name != 'rolling_baseline.py':
                    selected.add(path.relative_to(root).as_posix())

    manifest_path = _file(root, ASSET_MANIFEST)
    if manifest is None:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    selected.add(ASSET_MANIFEST)
    source_directory = manifest['source_directory']
    if source_directory != 'vendor/kronos':
        raise ValueError('manifest must use the pinned vendor/kronos source')
    selected.update(source_directory + '/' + entry['path'] for entry in manifest['source_files'])
    for checkpoint in manifest['checkpoints']:
        selected.update(
            'model-assets/kronos-small/' + entry['path']
            for entry in checkpoint['files']
            if include_weights or entry['path'].endswith('/config.json')
        )
    files = [_file(root, relative) for relative in sorted(selected)]
    return [path for path in files if path.is_file()]


def repository_files(root: Path, *, manifest: dict | None = None) -> set[str]:
    """Git publishes the source and download manifest, without checkpoint weights."""
    root = root.resolve()
    files = {path.relative_to(root).as_posix()
             for path in package_files(root, include_weights=False, manifest=manifest)}
    if _file(root, '.gitignore').is_file():
        files.add('.gitignore')
    return files
