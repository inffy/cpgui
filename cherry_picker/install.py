"""`cherry-picker --install` / `--uninstall`: per-user install, nothing system-wide."""
import os
import shlex
import shutil
import subprocess
import zipfile
from pathlib import Path

from .core import CACHE_DIR, CONFIG_FILE, DATA_DIR

BIN = Path.home() / ".local" / "bin" / "cherry-picker"
APPS = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "applications"
DESKTOP = APPS / "cherry-picker.desktop"

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=Cherry Picker
GenericName=Git Cherry-Pick Tool
Comment=Cherry-pick commits between branches of a GitHub repository
Exec={exec}
Icon=vcs-merge-request
Terminal=false
Categories=Development;RevisionControl;
Keywords=git;github;cherry-pick;backport;
StartupWMClass=cherry-picker
"""


def _refresh_menu():
    for tool in ("kbuildsycoca6", "update-desktop-database"):
        if shutil.which(tool):
            args = [tool] if tool == "kbuildsycoca6" else [tool, str(APPS)]
            subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def install(source):
    """Install from `source`: a cherry-picker.pyz file, or a source checkout (dev install)."""
    BIN.parent.mkdir(parents=True, exist_ok=True)
    if os.path.isfile(source) and zipfile.is_zipfile(source):
        if not (BIN.exists() and BIN.samefile(source)):
            tmp = BIN.with_name(BIN.name + ".new")
            shutil.copyfile(source, tmp)
            tmp.chmod(0o755)
            tmp.replace(BIN)
        how = f"copied {Path(source).name}"
    else:  # running from a git checkout: a launcher that runs the checkout, so edits apply directly
        if BIN.exists() or BIN.is_symlink():
            BIN.unlink()
        from .__main__ import system_python_command
        BIN.write_text("#!/bin/sh\nexec " + shlex.join(system_python_command()) + ' "$@"\n')
        BIN.chmod(0o755)
        how = f"launcher for the checkout in {source}"
    print(f"Installed {BIN} ({how})")

    APPS.mkdir(parents=True, exist_ok=True)
    DESKTOP.write_text(DESKTOP_ENTRY.format(exec=shlex.quote(str(BIN))))
    _refresh_menu()
    print(f"Added Cherry Picker to the application menu ({DESKTOP})")

    from . import pyside
    try:
        import PySide6  # noqa: F401
        print("Using the PySide6 that is already installed")
    except ImportError:
        if pyside.activate():
            print(f"Qt for Python is ready ({pyside.ROOT})")
        else:
            print("Warning: could not set up Qt for Python yet; run `cherry-picker --setup-qt` to retry.")

    if str(BIN.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"Note: {BIN.parent} is not on your PATH, so start it from the menu or with {BIN}")
    return 0


def uninstall():
    for path in (BIN, DESKTOP):
        if path.exists() or path.is_symlink():
            path.unlink()
            print(f"Removed {path}")
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
        print(f"Removed {DATA_DIR}")
    _refresh_menu()
    print(f"Kept your settings ({CONFIG_FILE.parent}) and repository clones ({CACHE_DIR}); "
          "delete them by hand if you no longer need them.")
    return 0
