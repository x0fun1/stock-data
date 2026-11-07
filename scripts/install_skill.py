#!/usr/bin/env python
"""Install the complete local stock-research skill with an isolated Python env."""
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
import tempfile
import urllib.request
import venv

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', '.pytest-tmp',
                 'docs', 'runtime', 'reports', 'cache', 'node_modules', '.install-tmp', '.pip-cache'}
EXCLUDED_FILES = {'inference.lock', 'install-receipt.json', '.env', '.secrets', 'rolling_baseline.py'}


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


def read_manifest(root: Path) -> tuple[Path, dict]:
    assets = root / 'model-assets/kronos-small'
    path = assets / 'manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('model_id') != 'kronos-small':
        raise ValueError('manifest is not the pinned Kronos-small package')
    return assets, manifest


def verify_source(root: Path, manifest: dict) -> None:
    source = bounded(root, manifest['source_directory'])
    tree = hashlib.sha256()
    for entry in manifest['source_files']:
        path = bounded(source, entry['path'])
        if not path.is_file() or sha(path) != entry['sha256']:
            raise ValueError(f'packaged official source hash mismatch: {entry["path"]}')
        tree.update((entry['path'] + '|' + entry['sha256'] + '\n').encode())
    if tree.hexdigest() != manifest['source_tree_sha256']:
        raise ValueError('source manifest tree hash mismatch')
    required = ('LICENSE', 'model/kronos.py', 'model/module.py', 'finetune_csv/finetune_base_model.py',
                'finetune_csv/finetune_tokenizer.py', 'finetune/train_predictor.py')
    # Keep the complete source tree; this check identifies the essential code.
    for rel in required:
        if not bounded(source, rel).is_file():
            raise ValueError(f'complete source is missing {rel}')


def copy_package(source: Path, destination: Path) -> int:
    copied = 0
    for current, dirs, files in os.walk(source, followlinks=False):
        here = Path(current)
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS and not (here / name).is_symlink()]
        relative_dir = here.relative_to(source)
        out_dir = bounded(destination, relative_dir.as_posix())
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in files:
            if name in EXCLUDED_FILES or name.endswith(('.pyc', '.pyo')):
                continue
            original = here / name
            if original.is_symlink():
                raise ValueError(f'package symlink not accepted: {original}')
            target = bounded(destination, (relative_dir / name).as_posix())
            if not target.exists() or sha(target) != sha(original):
                shutil.copy2(original, target)
                copied += 1
    return copied


def ensure_checkpoints(assets: Path, manifest: dict, offline: bool) -> dict:
    results = {}
    for checkpoint in manifest['checkpoints']:
        for entry in checkpoint['files']:
            path = bounded(assets, entry['path'])
            if path.exists():
                if sha(path) != entry['sha256']:
                    raise ValueError(f'existing checkpoint is corrupt: {entry["path"]}')
                results[entry['path']] = 'bundled_verified'
                continue
            if offline:
                raise ValueError(f'checkpoint absent in offline install: {entry["path"]}')
            path.parent.mkdir(parents=True, exist_ok=True)
            expected_url = (f'https://huggingface.co/{checkpoint["repo"]}/resolve/'
                            f'{checkpoint["revision"]}/{Path(entry["path"]).name}')
            if entry['url'] != expected_url:
                raise ValueError('checkpoint download URL is not bound to the pinned revision')
            request = urllib.request.Request(expected_url, headers={'User-Agent': 'stock-research-installer'})
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.download-', delete=False) as output:
                temporary = Path(output.name)
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        shutil.copyfileobj(response, output)
                except Exception:
                    output.close()
                    temporary.unlink(missing_ok=True)
                    raise
            if sha(temporary) != entry['sha256'] or temporary.stat().st_size != entry['bytes']:
                temporary.unlink(missing_ok=True)
                raise ValueError(f'download checkpoint hash/size mismatch: {entry["path"]}')
            os.replace(temporary, path)
            results[entry['path']] = 'pinned_download_verified'
    return results


def environment_python(destination: Path) -> Path:
    return destination / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def install_environment(destination: Path) -> dict:
    temp_dir = bounded(destination, '.install-tmp')
    cache_dir = bounded(destination, '.pip-cache')
    temp_dir.mkdir(exist_ok=True)
    cache_dir.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        env[name] = str(temp_dir)
    env['PIP_CACHE_DIR'] = str(cache_dir)
    return env


def run(command: list[str], cwd: Path, env: dict | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def smoke(python: Path, destination: Path) -> dict:
    code = r'''
import json,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path.cwd()/'scripts'))
from stock_research.models.kronos import forecast_kronos
import numpy as np
import pandas as pd
import lightgbm as lgb
from stock_research.multifactor.preprocessing import prepare_cross_section
from stock_research.multifactor.catalog import group_equal_weight_score
from stock_research.multifactor.study import run_multifactor_study, load_candidate
history=[]
for i in range(4):
    start=datetime(2026,11,5,15+i,tzinfo=timezone.utc)
    price=180+i*.3
    history.append({'date':f'2026-11-05T{10+i:02d}:00:00','start_utc':start.isoformat(),
                    'end_utc':(start+timedelta(hours=1)).isoformat(),'market_date':'2026-11-05',
                    'open':price,'high':price+.5,'low':price-.4,'close':price+.1,
                    'volume':400000+i*1000,'amount':(400000+i*1000)*(price+.05)})
result=forecast_kronos(history,runtime_root=Path.cwd()/'runtime',config={},ticker='SELFTEST',
                       frequency='1h',cutoff='2026-11-05T19:00:00+00:00',
                       target_start='2026-11-05T19:00:00+00:00',target_end='2026-11-05T20:00:00+00:00')
if result.get('status')!='ok':
    raise RuntimeError('real pinned-model self-test failed: '+json.dumps(result,ensure_ascii=False))
if result.get('n_history')!=4 or result.get('history_truncated'):
    raise RuntimeError('unexpected self-test history policy')
Path('runtime').mkdir(exist_ok=True)
Path('runtime/install-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
rng=np.random.default_rng(20261107)
X=rng.normal(size=(80,3))
y=.02*X[:,0]-.01*X[:,1]
booster=lgb.train({'objective':'regression','verbosity':-1,'num_threads':2,
    'num_leaves':7,'min_data_in_leaf':5,'seed':20261107,'deterministic':True,'force_col_wise':True},
    lgb.Dataset(X,label=y),num_boost_round=8)
pred=booster.predict(X)
if not np.isfinite(pred).all() or np.std(pred)<=0 or booster.num_trees()!=8:
    raise RuntimeError('native LightGBM fit/predict self-test failed')
section=pd.DataFrame({'security_id':[f'S{i}' for i in range(24)],
    'cutoff_date':'2025-01-02','industry':['a']*12+['b']*12,
    'industry_pit_confirmed':True,'size_pit_confirmed':True,
    'f__size_log':rng.normal(25,1,24),'f__mom_60':rng.normal(size=24),
    'f__ep_ttm':rng.normal(.05,.01,24)})
normalized=prepare_cross_section(section,['mom_60','ep_ttm'],neutralize=True)['frame']
scores=group_equal_weight_score(normalized,['mom_60','ep_ttm'],column_prefix='n__')
if scores.isna().any():
    raise RuntimeError('native factor normalization/neutralization self-test failed')
multifactor={'status':'passed','library_version':lgb.__version__,'actual_trained_trees':booster.num_trees(),
    'cross_section_rows':len(section),'model_loader':'native_json_and_lightgbm_text',
    'synthetic_functional_check':True,'probability_verified':False}
Path('runtime/install-multifactor-smoke.json').write_text(json.dumps(multifactor,indent=2),encoding='utf-8')
print(json.dumps({'model_id':result['model_id'],'status':result['status'],
                  'n_history':result['n_history'],'paths':result.get('path_count'),
                  'multifactor':multifactor,'prediction_is_calibrated_probability':False}))
'''
    result = subprocess.run([str(python), '-c', code], cwd=destination, env=install_environment(destination), check=True,
                            capture_output=True, text=True, encoding='utf-8')
    return {'status': 'passed', 'result': json.loads(result.stdout.splitlines()[-1]),
            'artifact': 'runtime/install-smoke.json'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True, type=Path, help='empty target skill directory')
    parser.add_argument('--update', action='store_true', help='overwrite matching package files in an existing installation; never deletes user files')
    parser.add_argument('--offline', action='store_true', help='require bundled weights; do not download checkpoints')
    parser.add_argument('--skip-deps', action='store_true', help='reuse the invoking Python for an existing prepared environment')
    parser.add_argument('--skip-smoke', action='store_true', help='copy/verify only, without calling the actual model')
    parser.add_argument('--torch-index-url', default='https://download.pytorch.org/whl/cpu',
                        help='CPU PyTorch wheel index or a compatible trusted CPU mirror')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 12):
        raise SystemExit('Python 3.12 or newer is required by the pinned dependency lock; no files have been installed')
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
    assets, manifest = read_manifest(source)
    verify_source(source, manifest)
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0 if inplace else copy_package(source, destination)
    installed_assets, installed_manifest = read_manifest(destination)
    verify_source(destination, installed_manifest)
    checkpoints = ensure_checkpoints(installed_assets, installed_manifest, args.offline)
    python = Path(sys.executable)
    if not args.skip_deps:
        env_dir = destination / '.venv'
        if not environment_python(destination).is_file():
            venv.EnvBuilder(with_pip=False, clear=False).create(env_dir)
        python = environment_python(destination)
        env = install_environment(destination)
        lock = destination / 'requirements-kronos.lock.txt'
        if not lock.is_file():
            raise SystemExit('verified Kronos dependency lock file is missing')
        pip_check = subprocess.run([str(python), '-m', 'pip', '--version'], cwd=destination,
                                   env=env, capture_output=True)
        if pip_check.returncode:
            run([str(python), '-m', 'ensurepip', '--upgrade', '--default-pip'], destination, env)
        run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
             'torch==2.8.0', '--index-url', args.torch_index_url, '-c', str(lock)], destination, env)
        run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
             '-r', str(destination / 'requirements-kronos.txt'), '-c', str(lock)], destination, env)
    model_smoke = {'status': 'skipped'} if args.skip_smoke else smoke(python, destination)
    receipt = {'installed_at': datetime.now(timezone.utc).isoformat(), 'source_commit': manifest['source_commit'],
               'model_manifest_sha256': sha(installed_assets / 'manifest.json'), 'copied_files': copied,
               'dependency_lock_sha256': sha(destination / 'requirements-kronos.lock.txt'),
               'checkpoints': checkpoints, 'python_environment': '.venv' if not args.skip_deps else 'invoking_environment',
               'smoke': model_smoke, 'global_python_modified': False}
    (destination / 'install-receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'installed', 'destination': str(destination),
                      'python': str(python), 'smoke': model_smoke['status'], 'copied_files': copied}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
