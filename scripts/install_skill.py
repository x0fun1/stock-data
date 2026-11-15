#!/usr/bin/env python
"""Install the local stock-research news skill with an isolated Python env."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import venv

from stock_research.common.package_files import package_files

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def bounded(root: Path, relative: str) -> Path:
    rel = Path(relative)
    target = root / rel
    if rel.is_absolute() or '..' in rel.parts or not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'unsafe package path: {relative}')
    return target


def copy_package(source: Path, destination: Path) -> int:
    copied = 0
    source = source.resolve()
    for original in package_files(source):
        target = bounded(destination, original.relative_to(source).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or sha(target) != sha(original):
            shutil.copy2(original, target)
            copied += 1
    return copied


def environment_python(destination: Path) -> Path:
    return destination / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def install_environment(destination: Path) -> dict:
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    # No pip cache or temp directory is left inside the installed skill.
    env['PIP_NO_CACHE_DIR'] = '1'
    return env


def run(command: list[str], cwd: Path, env: dict | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def smoke(python: Path, destination: Path) -> dict:
    """Run the real import -> review -> report path on a temporary data root."""
    code = r'''
import json,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'scripts'))
from stock_research.contracts import ResearchRequest
from stock_research.control.news_reviews import import_news_reviews
from stock_research.control.orchestration import run_research
review={'title':'Self-test partnership','security_ids':['US:XNAS:SELFTEST'],'event_cluster':'selftest',
    'fact_summary':'Synthetic installer check.','grade':'中度利好','grade_reason':'Synthetic.',
    'reviewed_at':'2026-11-07T10:00:00Z','evidence_scope':'official_excerpt',
    'impact_assessment':{'direction_sign':1,'impact_level':'medium','event_kind':'partnership',
        'event_time':'2026-11-05T15:00:00Z','stock_relevance':1.0,'evidence_confidence':0.8},
    'sources':[{'url':'https://example.com/selftest','published_at':'2026-11-05T15:00:00Z','evidence_locator':'paragraph 1'}]}
with tempfile.TemporaryDirectory() as folder:
    path=Path(folder)/'review.json';path.write_text(json.dumps([review]),encoding='utf-8')
    import_news_reviews(path,Path(folder)/'data')
    bundle,markdown=run_research(ResearchRequest(market='US',tickers=['SELFTEST'],cutoff='2026-11-08T00:00:00+00:00'),
        runtime_root=Path(folder)/'data')
impact=bundle.news_impact_summary
if impact['status']!='computed' or impact['classification']!='利多' or '中度利好' not in markdown:
    raise RuntimeError('news review/report self-test failed')
print(json.dumps({'status':'ok','events':impact['event_count'],'classification':impact['classification']},ensure_ascii=False))
'''
    result = subprocess.run([str(python), '-c', code], cwd=destination, env=install_environment(destination),
                            capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError('installation self-test failed:\n' + result.stderr[-8000:])
    return {'status': 'passed', 'result': json.loads(result.stdout.splitlines()[-1])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path, help='empty target skill directory')
    parser.add_argument('--update', action='store_true', help='overwrite matching package files in an existing installation; never deletes user files')
    parser.add_argument('--skip-deps', action='store_true', help='reuse the invoking Python for an existing prepared environment')
    parser.add_argument('--skip-smoke', action='store_true', help='copy only, without running the self-test')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 or newer is required; no files have been installed')
    source = PACKAGE_ROOT.resolve()
    destination = args.destination.expanduser().resolve()
    inplace = destination == source
    if inplace and not args.update:
        raise SystemExit('in-place installation requires explicit --update')
    if not inplace and (destination.is_relative_to(source) or source.is_relative_to(destination)):
        raise SystemExit('destination must be outside the source skill and cannot be an ancestor of it')
    if destination.exists() and not destination.is_dir():
        raise SystemExit('destination is not a directory')
    if destination.exists() and any(destination.iterdir()) and not args.update:
        raise SystemExit('destination is not empty; select an empty target or explicitly use --update')
    if destination.exists() and any(destination.iterdir()) and args.update:
        skill_entry = destination / 'SKILL.md'
        if not skill_entry.is_file() or not re.search(r'^name:\s*stock-research\s*$', skill_entry.read_text(encoding='utf-8'), re.MULTILINE):
            raise SystemExit('--update only accepts an existing stock-research skill directory')
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0 if inplace else copy_package(source, destination)
    python = Path(sys.executable)
    if not args.skip_deps:
        if not environment_python(destination).is_file():
            venv.EnvBuilder(with_pip=True, clear=False).create(destination / '.venv')
        python = environment_python(destination)
        run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
             '-r', str(destination / 'requirements.txt')], destination, install_environment(destination))
    result = {'status': 'skipped'} if args.skip_smoke else smoke(python, destination)
    receipt = {'installed_at': datetime.now(timezone.utc).isoformat(), 'copied_files': copied,
               'requirements_sha256': sha(destination / 'requirements.txt'),
               'python_environment': '.venv' if not args.skip_deps else 'invoking_environment',
               'smoke': result, 'global_python_modified': False}
    (destination / 'install-receipt.json').write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({'status': 'installed', 'destination': str(destination),
                      'python': str(python), 'smoke': result['status'], 'copied_files': copied}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
