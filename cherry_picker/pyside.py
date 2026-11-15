"""Use Fedora's PySide6 without installing or layering it.

The python3-pyside6 / python3-shiboken6 RPMs that match the system Qt are downloaded with
`dnf download` (as your user), signature-checked with `rpm -K` and unpacked into
~/.local/share/cherry-picker/pyside/. Because they are built against the system Qt, the app gets the real
Plasma integration: your widget style (Breeze/Union), colour scheme, icons and KDE dialogs.

`cherry-picker --setup-qt` (re)fetches it by hand; deleting that folder removes it.
"""
import ctypes
import glob
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import DATA_DIR

ROOT = DATA_DIR / "pyside"
PACKAGES = ("python3-pyside6", "python3-shiboken6")


def _minor(version):
    return ".".join(version.split(".")[:2]) if version else None


def system_qt_version():
    """e.g. '6.11.2', read from the libQt6Core.so.6 symlink target."""
    for d in ("/usr/lib64", "/usr/lib"):
        m = re.search(r"\.so\.(\d+\.\d+\.\d+)$", os.path.realpath(f"{d}/libQt6Core.so.6"))
        if m:
            return m.group(1)
    return None


def local_version():
    eggs = glob.glob(f"{ROOT}/usr/lib*/python3*/site-packages/PySide6-*.egg-info")
    m = re.search(r"PySide6-([\d.]+)-py", eggs[0]) if eggs else None
    return m.group(1) if m else None


def _best_available(qt_minor):
    """Newest python3-pyside6 in the Fedora repos built for this Qt minor version."""
    out = subprocess.run(["dnf", "repoquery", "-q", "--qf", "%{version}\n", "--arch", platform.machine(),
                          "python3-pyside6"], capture_output=True, text=True, timeout=180).stdout
    versions = {v.strip() for v in out.split() if _minor(v.strip()) == qt_minor}
    return max(versions, key=lambda v: tuple(map(int, v.split(".")))) if versions else None


def install(log=lambda s: print(s, file=sys.stderr)):
    qt = system_qt_version()
    if not qt:
        raise RuntimeError("could not find the system Qt 6")
    ver = _best_available(_minor(qt))
    if not ver:
        raise RuntimeError(f"the Fedora repositories have no python3-pyside6 for Qt {_minor(qt)}")
    log(f"Fetching Fedora's PySide6 {ver} for Qt {qt} (one-time, about 11 MB)…")
    arch = platform.machine()
    with tempfile.TemporaryDirectory(prefix="cherry-picker-") as tmp:
        subprocess.run(["dnf", "download", "-q", f"--destdir={tmp}", "--arch", arch]
                       + [f"{p}-{ver}" for p in PACKAGES], check=True, timeout=600)
        rpms = sorted(glob.glob(f"{tmp}/*.rpm"))
        if len(rpms) != len(PACKAGES):
            raise RuntimeError(f"expected {len(PACKAGES)} packages, got {[Path(r).name for r in rpms]}")
        for rpm in rpms:  # verify against the Fedora keys the system trusts
            check = subprocess.run(["rpm", "-K", rpm], capture_output=True, text=True)
            if check.returncode or "signatures OK" not in check.stdout:
                raise RuntimeError(f"signature check failed for {Path(rpm).name}: {check.stdout.strip()}")
        new = Path(tmp) / "root"
        new.mkdir()
        for rpm in rpms:
            cpio = subprocess.Popen(["cpio", "-idm", "--quiet"], cwd=new, stdin=subprocess.PIPE)
            subprocess.run(["rpm2cpio", rpm], stdout=cpio.stdin, check=True)
            cpio.stdin.close()
            if cpio.wait():
                raise RuntimeError(f"could not unpack {Path(rpm).name}")
        ROOT.parent.mkdir(parents=True, exist_ok=True)
        if ROOT.exists():
            shutil.rmtree(ROOT)
        shutil.move(str(new), ROOT)
    log(f"PySide6 {ver} unpacked into {ROOT}")
    return ver


def activate(auto_install=True):
    """Make the unpacked PySide6 importable in this interpreter. Returns True on success."""
    qt = system_qt_version()
    if not qt:
        return False
    if _minor(local_version()) != _minor(qt):  # Qt's private ABI is tied to the minor version
        if not auto_install or not shutil.which("dnf"):
            return False
        try:
            install()
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            print(f"cherry-picker: could not fetch a matching PySide6: {e}", file=sys.stderr)
            return False
    py = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = glob.glob(f"{ROOT}/usr/lib*/{py}/site-packages")
    libs = [sorted(glob.glob(f"{ROOT}/usr/lib*/{name}.cpython-*.so.*"), key=len) for name in ("libshiboken6", "libpyside6")]
    if not site or not all(libs):
        return False  # built for another Python version
    try:  # Fedora's modules find these via the system library path, so preload them
        for candidates in libs:
            ctypes.CDLL(candidates[0], mode=ctypes.RTLD_GLOBAL)
    except OSError:
        return False
    sys.path[:0] = site
    return True

