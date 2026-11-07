#!/usr/bin/env python
"""Build a portable source + weights ZIP without machine caches or research data."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', '.venv', '__pycache__', '.pytest_cache', '.pytest-tmp',
             'docs', 'runtime', 'reports', 'cache', 'node_modules', '.install-tmp', '.pip-cache'}
SKIP_FILES = {'inference.lock', 'install-receipt.json', '.env', '.secrets',
              '.gitignore', '.gitattributes', 'rolling_baseline.py'}


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if output.is_relative_to(ROOT.resolve()):
        raise SystemExit('bundle output must be outside the source skill directory')
    files = []
    for path in sorted(ROOT.rglob('*')):
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in relative.parts) or path.name in SKIP_FILES:
            continue
        if path.is_symlink():
            raise SystemExit('bundle does not accept symlinks: ' + str(relative))
        if not path.is_file() or path.suffix in {'.pyc', '.pyo'}:
            continue
        files.append({'path': relative.as_posix(), 'bytes': path.stat().st_size, 'sha256': sha(path)})
    required = {'SKILL.md', 'scripts/install_skill.py', 'scripts/skill.py',
                'vendor/kronos/LICENSE', 'model-assets/kronos-small/manifest.json',
                'model-assets/kronos-small/small/model.safetensors',
                'model-assets/kronos-small/tokenizer/model.safetensors',
                'references/kronos-training-plan.md', 'references/multifactor-workflow.md',
                'scripts/stock_research/multifactor/study.py',
                'scripts/stock_research/multifactor/risk.py',
                'scripts/stock_research/control/multifactor.py'}
    if not required <= {entry['path'] for entry in files}:
        raise SystemExit('portable bundle is missing required code, weights or training plan')
    manifest = {'skill': 'stock-research', 'package_prefix': 'stock-research/',
                'files': files, 'bundled_weights': True,
                'dependencies': 'installer creates a package-local venv; first install requires package access'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for entry in files:
            z.write(ROOT / entry['path'], 'stock-research/' + entry['path'])
        z.writestr('stock-research/bundle-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(output) as z:
        failure = z.testzip()
        if failure:
            raise SystemExit('bundle CRC failed: ' + failure)
    receipt = {'archive': str(output), 'bytes': output.stat().st_size,
               'sha256': sha(output), 'source_files': len(files), 'all_crc_passed': True}
    output.with_suffix('.receipt.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
