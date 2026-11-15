#!/usr/bin/env python3
"""Build the single-file app: dist/cherry-picker.pyz

    python3 build.py [VERSION]

VERSION defaults to `git describe --tags` (without the leading "v").
Only the standard library is needed; the .pyz contains just the cherry_picker package.
"""
import re
import shutil
import subprocess
import sys
import tempfile
import zipapp
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "dist" / "cherry-picker.pyz"


def version():
    if len(sys.argv) > 1:
        return sys.argv[1].removeprefix("v")
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"], cwd=HERE,
                             capture_output=True, text=True, check=True).stdout.strip()
        return out.removeprefix("v")
    except (OSError, subprocess.CalledProcessError):
        return re.search(r'__version__ = "(.*)"', (HERE / "cherry_picker" / "__init__.py").read_text()).group(1)


def main():
    ver = version()
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "app"
        shutil.copytree(HERE / "cherry_picker", stage / "cherry_picker",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        init = stage / "cherry_picker" / "__init__.py"
        init.write_text(re.sub(r'__version__ = "[^"]*"', f'__version__ = "{ver}"', init.read_text()))
        (stage / "__main__.py").write_text("from cherry_picker.__main__ import main\n\nraise SystemExit(main())\n")
        OUT.parent.mkdir(exist_ok=True)
        zipapp.create_archive(stage, OUT, interpreter="/usr/bin/python3", compressed=True)
    print(f"Built {OUT.relative_to(HERE)} (version {ver}, {OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
