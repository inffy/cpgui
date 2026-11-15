"""Entry point for `python3 -m cherry_picker` and the single-file cherry-picker.pyz."""
import argparse
import os
import sys

from . import __version__

SYSTEM_PY = "/usr/bin/python3"
# Run the cherry_picker package found at sys.argv[1] (a checkout dir or the .pyz), not one from the cwd.
BOOTSTRAP = ("import sys; root = sys.argv.pop(1); sys.path.insert(0, root); "
             "from cherry_picker.__main__ import main; sys.exit(main())")


def package_root():
    """The directory - or .pyz file - that contains the cherry_picker package."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def system_python_command(*args):
    return [SYSTEM_PY, "-c", BOOTSTRAP, package_root(), *args]


def ensure_system_python():
    """Fedora's PySide6 is built for the system Python; switch to it (e.g. from Homebrew's python3)."""
    if sys.prefix == "/usr" or os.environ.pop("CHERRY_PICKER_REEXEC", None) or not os.path.exists(SYSTEM_PY):
        return
    os.environ["CHERRY_PICKER_REEXEC"] = "1"
    os.execv(SYSTEM_PY, system_python_command(*sys.argv[1:]))


def load_qt():
    try:  # a system-wide PySide6, if there is one
        import PySide6.QtWidgets  # noqa: F401
        return True
    except ImportError:
        pass
    from . import pyside
    if not pyside.activate():
        return False
    try:
        import PySide6.QtWidgets  # noqa: F401
        return True
    except ImportError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cherry-picker",
        description="Cherry-pick commits between branches of a GitHub repository.")
    parser.add_argument("repo", nargs="?", help="repository to open: owner/repo, a GitHub URL or any git URL")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true",
                        help="install to ~/.local/bin and add it to the application menu")
    action.add_argument("--uninstall", action="store_true", help="remove what --install added")
    action.add_argument("--setup-qt", action="store_true",
                        help="(re)fetch Fedora's PySide6 that matches the system Qt")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    ensure_system_python()
    if args.install or args.uninstall:
        from . import install
        return install.install(package_root()) if args.install else install.uninstall()
    if args.setup_qt:
        from . import pyside
        pyside.install()
        return 0
    if not load_qt():
        sys.exit("cherry-picker: could not load Qt for Python (PySide6).\n"
                 "On Fedora-based systems run `cherry-picker --setup-qt`; elsewhere install PySide6 "
                 "for the system Python (e.g. your distribution's python3-pyside6 package).")
    from .app import run
    return run(args.repo)


if __name__ == "__main__":
    sys.exit(main())
