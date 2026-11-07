#!/usr/bin/env python
"""Use this installed skill's Python environment, without global dependencies."""
from pathlib import Path
import os
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    executable = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not executable.is_file():
        print('Skill依赖环境尚未安装。请先运行 scripts/install_skill.py 安装完整skill。', file=sys.stderr)
        return 2
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    return subprocess.call([str(executable), str(root / 'scripts/research.py'), *sys.argv[1:]], env=env)


if __name__ == '__main__':
    raise SystemExit(main())
