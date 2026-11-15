"""Select the current skill source, independently of local research directories."""
from __future__ import annotations

import os
from pathlib import Path


ROOT_FILES = {'README.md', 'SKILL.md', 'pyproject.toml', 'requirements.txt'}
SOURCE_DIRS = {
    'scripts': {'.py'}, 'tests': {'.py'},
    'references': {'.md'}, 'policies': {'.json'},
}
EXAMPLE_FILES = {'news-template.json', 'macro-template.csv', 'request-hk.json', 'request-us.json'}
LOCAL_DIRS = {
    '.git', '.venv', '__pycache__', '.pytest_cache', '.pytest-tmp', '.ruff_cache',
    '.install-tmp', '.pip-cache', 'docs', 'runtime', 'reports', 'cache', 'node_modules',
}


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError(f'unsafe package path: {relative}')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'package path leaves source: {relative}')
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
        raise ValueError(f'package symlink not accepted: {relative}')
    return path


def package_files(root: Path) -> list[Path]:
    """Only declared first-party files enter a package; local output never expands it."""
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
                if path.suffix in suffixes:
                    selected.add(path.relative_to(root).as_posix())
    files = [_file(root, relative) for relative in sorted(selected)]
    return [path for path in files if path.is_file()]


def repository_files(root: Path) -> set[str]:
    """Git publishes exactly the package source plus the shared ignore file."""
    root = root.resolve()
    files = {path.relative_to(root).as_posix() for path in package_files(root)}
    if _file(root, '.gitignore').is_file():
        files.add('.gitignore')
    return files
