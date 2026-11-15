"""Seven-day retention for generated runtime artifacts, coordinated with active runs."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from functools import wraps
import inspect
import os
from pathlib import Path
import sqlite3
import stat

from ..common.jsonio import read_json, write_json
from ..common.timeutil import ensure_utc, now_utc
from ..config import default_runtime_root, default_skill_root

RETENTION_DAYS = 7
_ACTIVE = ContextVar('runtime_retention', default=None)
_MANAGED = {'raw', 'curated', 'reports', 'news-workflows', 'cache', 'tmp', 'temp', 'acceptance'}
_AUXILIARY = ('*headlines*.json', '*summary*.json', '*benchmark*.json', '*benchmark*.md',
              '*validation*.json', '*receipt*.json', '*result*.json', '*.log', '*.tmp')


def _scope(runtime_root):
    requested = Path(runtime_root).expanduser()
    if requested.is_symlink():
        raise ValueError('runtime cleanup does not accept a symlink root')
    root = requested.resolve()
    if root == Path(root.anchor) or root == Path.home().resolve() or (root / '.git').exists() or (root / 'SKILL.md').exists():
        raise ValueError('runtime cleanup requires a dedicated data directory, not a home or source directory')
    bundled = (default_skill_root() / 'runtime').resolve()
    # All per-analysis subdirectories inside this skill share one retention boundary.
    return bundled if root.is_relative_to(bundled) else root


@contextmanager
def _lock(scope):
    scope.mkdir(parents=True, exist_ok=True)
    path = scope / '.retention.lock'
    if path.is_symlink():
        raise ValueError('runtime retention lock cannot be a symlink')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a+b') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('runtime retention lock must be a regular file')
        if os.name == 'nt':
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _snapshot(path):
    """Do not follow symlinks, including any embedded in a directory group."""
    if path.is_symlink():
        return None
    if path.is_file():
        info = path.lstat()
        return [(path, info)] if stat.S_ISREG(info.st_mode) else None
    if not path.is_dir():
        return []
    entries = []
    for current, dirs, files in os.walk(path, followlinks=False):
        here = Path(current)
        if any((here / name).is_symlink() for name in dirs + files):
            return None
        for name in files:
            item = here / name
            info = item.lstat()
            if not stat.S_ISREG(info.st_mode):
                return None
            entries.append((item, info))
    return entries


def _roots(scope):
    """Discover nested runtime roots without descending into datasets or source."""
    yield scope
    for current, dirs, _ in os.walk(scope, followlinks=False):
        here = Path(current)
        dirs[:] = sorted(name for name in dirs if name not in _MANAGED and not name.startswith('.')
                         and name not in {'scripts', 'tests', 'policies', 'references', 'docs', 'node_modules'}
                         and not (here / name).is_symlink())
        if here != scope and ((here / 'catalog.sqlite').is_file()
                             or any((here / name).is_dir() for name in _MANAGED)):
            yield here


def _groups(root, cutoff):
    for name in ('raw', 'reports', 'curated'):
        folder = root / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        grouped = {}
        for current, dirs, files in os.walk(folder, followlinks=False):
            here = Path(current)
            dirs[:] = [entry for entry in dirs if not (here / entry).is_symlink()]
            for entry in files:
                path = here / entry
                if path.is_symlink():
                    continue
                if name == 'raw':
                    key = str(path).removesuffix('.manifest.json')
                elif name == 'reports':
                    key = str(path.with_suffix(''))
                else:
                    key = str(path)
                grouped.setdefault(key, []).append(path)
        yield from grouped.values()
    workflows = root / 'news-workflows'
    if workflows.is_dir() and not workflows.is_symlink():
        for folder in sorted(workflows.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            snapshot = _snapshot(folder)
            if snapshot is None:
                continue
            if snapshot and max(info.st_mtime for _, info in snapshot) < cutoff:
                yield [folder]
            else:
                # A new collection must not keep all older collections alive forever.
                for name in ('collections', 'plugin-batches'):
                    batches = folder / name
                    if batches.is_dir() and not batches.is_symlink():
                        yield from ([batch] for batch in sorted(batches.iterdir()) if not batch.is_symlink())
    for name in ('cache', 'tmp', 'temp', 'acceptance'):
        folder = root / name
        if folder.is_dir() and not folder.is_symlink():
            yield from ([path] for path in sorted(folder.iterdir()) if not path.is_symlink())
    auxiliary = {path for pattern in _AUXILIARY for path in root.glob(pattern)}
    for path in sorted(auxiliary):
        if path.is_file() and not path.is_symlink() and not path.name.startswith('.'):
            yield [path]


def _local_path(value, root):
    path = Path(value)
    if not path.is_absolute():
        cwd_path = path.resolve()
        path = cwd_path if cwd_path.is_relative_to(root) else root / path
    # Reject both resolved escapes and symlink ancestors.
    if not path.resolve().is_relative_to(root):
        return None
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            return None
    return path


def _reconcile_catalog(root, dry_run, planned_files):
    path = root / 'catalog.sqlite'
    if not path.is_file() or path.is_symlink():
        return 0
    conn = sqlite3.connect(path.as_uri() + ('?mode=ro' if dry_run else '?mode=rw'), uri=True, timeout=30)
    removed = 0
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, keys in (('datasets', ('name', 'version')), ('runs', ('run_id',))):
            if table not in tables:
                continue
            columns = ','.join((*keys, 'path'))
            for row in conn.execute(f'SELECT {columns} FROM {table}').fetchall():
                target = _local_path(row[-1], root)
                if target is None or (target.exists() and target not in planned_files):
                    continue
                removed += 1
                if not dry_run:
                    conn.execute(f'DELETE FROM {table} WHERE ' + ' AND '.join(f'{key}=?' for key in keys), row[:-1])
        if not dry_run:
            conn.commit()
            if removed:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                conn.execute('VACUUM')
    finally:
        conn.close()
    return removed


def _cleanup(scope, requested_at, dry_run=False):
    cutoff = requested_at - timedelta(days=RETENTION_DAYS)
    result = {'retention_days': RETENTION_DAYS, 'requested_at': requested_at.isoformat(),
              'cutoff': cutoff.isoformat(), 'scope': str(scope), 'dry_run': dry_run,
              'expired_files': 0, 'expired_bytes': 0, 'deleted_files': 0, 'freed_bytes': 0,
              'catalog_entries_removed': 0, 'errors': []}
    roots = list(_roots(scope))
    planned_files = set()
    def remove(path, info):
        if path in planned_files:
            return
        planned_files.add(path)
        result['expired_files'] += 1
        result['expired_bytes'] += info.st_size
        if not dry_run:
            path.unlink()
            result['deleted_files'] += 1
            result['freed_bytes'] += info.st_size
    for root in roots:
        for group in _groups(root, cutoff.timestamp()):
            try:
                snapshots = [_snapshot(path) for path in group]
                if any(value is None for value in snapshots):
                    continue
                entries = [entry for value in snapshots for entry in value]
                if not entries or any(info.st_mtime >= cutoff.timestamp() for _, info in entries):
                    continue
                # Recheck as a group before deletion, in case an external writer changed it.
                current = [_snapshot(path) for path in group]
                if current != snapshots:
                    continue
                for path, info in entries:
                    remove(path, info)
            except OSError as error:
                result['errors'].append({'path': str(group[0].relative_to(scope)), 'reason': type(error).__name__})
        pointers = root / 'news-workflows'
        if pointers.is_dir() and not pointers.is_symlink():
            for pointer in pointers.glob('*-latest.json'):
                if pointer.is_symlink():
                    continue
                try:
                    target = _local_path(read_json(pointer)['folder'], root)
                    if target and (not (target / 'tasks.json').is_file() or target / 'tasks.json' in planned_files):
                        remove(pointer, pointer.stat())
                except (OSError, ValueError, KeyError, TypeError) as error:
                    result['errors'].append({'path': str(pointer.relative_to(scope)), 'reason': type(error).__name__})
        try:
            result['catalog_entries_removed'] += _reconcile_catalog(root, dry_run, planned_files if dry_run else set())
        except (OSError, sqlite3.Error) as error:
            result['errors'].append({'path': str(root.relative_to(scope)), 'reason': type(error).__name__})
    if not dry_run:
        # Remove empty generated subdirectories, but retain the runtime and lock.
        for root in roots:
            for name in _MANAGED:
                folder = root / name
                if folder.is_dir() and not folder.is_symlink():
                    for current, _, _ in os.walk(folder, topdown=False, followlinks=False):
                        path = Path(current)
                        if not path.is_symlink():
                            try:
                                path.rmdir()
                            except OSError:
                                pass
        write_json(scope / '.retention-last.json', result)
    return result


def cleanup_runtime(runtime_root, requested_at=None, dry_run=False):
    """Inspect or remove expired files. No deleted payloads are copied elsewhere."""
    scope = _scope(runtime_root)
    stamp = ensure_utc(requested_at or now_utc())
    with _lock(scope):
        return _cleanup(scope, stamp, dry_run)


@contextmanager
def runtime_session(runtime_root, requested_at=None):
    """Clean once per request and keep cleanup from racing with an active operation."""
    scope = _scope(runtime_root)
    active = _ACTIVE.get()
    if active and active['scope'] == str(scope):
        yield active
        return
    stamp = ensure_utc(requested_at or now_utc())
    with _lock(scope):
        receipt = _cleanup(scope, stamp)
        token = _ACTIVE.set(receipt)
        try:
            yield receipt
        finally:
            _ACTIVE.reset(token)


def current_retention():
    return _ACTIVE.get()


def retained_runtime(argument='runtime_root'):
    """Apply the same lifecycle to direct Python entry points and nested CLI calls."""
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            root = bound.arguments.get(argument) or default_runtime_root()
            with runtime_session(root):
                return function(*args, **kwargs)
        return wrapped
    return decorate
