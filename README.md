# Cherry Picker

A small Qt app for cherry-picking commits between branches of a GitHub repository.
Pick a source and a target branch, tick the commits, and it creates a branch such as
`123-to-stable` (named after the PRs the commits came from), applies them and helps you
through conflicts. It can then push the branch and open the pull request page.

The repository is cloned into `~/.cache/cherry-picker/`, so your own checkouts are never touched.

## Install

Needs a Fedora-based system (Fedora, Aurora, Bazzite, Kinoite, …) with `git`.
Nothing is installed system-wide and no packages are layered.

```bash
curl -LO https://github.com/inffy/cpgui/releases/latest/download/cherry-picker.pyz
python3 cherry-picker.pyz --install
```

This puts `cherry-picker` in `~/.local/bin`, adds **Cherry Picker** to the application menu
and fetches Qt for Python (see below). The downloaded `.pyz` can be deleted afterwards.

**Update:** run the same two commands again.
**Remove:** `cherry-picker --uninstall`

For private repositories, log in once with `gh auth login` (GitHub CLI), or use an SSH URL
such as `git@github.com:owner/repo.git`.

## How the native Plasma look works

Only a Qt for Python (PySide6) build that matches the system Qt can load Plasma's widget style,
colour scheme and KDE integration. So on first run the app downloads Fedora's own
`python3-pyside6` and `python3-shiboken6` RPMs as your user with `dnf download`, checks their
signatures with `rpm -K`, and unpacks them into `~/.local/share/cherry-picker/pyside/`.
When a system update moves Qt to a new 6.x version, a matching build is fetched automatically.
`cherry-picker --setup-qt` re-fetches it by hand.

## Development

```bash
git clone git@github.com:inffy/cpgui.git && cd cpgui
python3 -m cherry_picker            # run from the checkout
python3 -m cherry_picker --install  # menu entry that runs this checkout
python3 build.py                    # builds dist/cherry-picker.pyz
```

| File | What it does |
|---|---|
| `cherry_picker/app.py` | Qt main window |
| `cherry_picker/core.py` | git / GitHub logic and settings |
| `cherry_picker/pyside.py` | fetches Fedora's PySide6 for the system Qt |
| `cherry_picker/install.py` | `--install` / `--uninstall` |
| `cherry_picker/__main__.py` | command line and start-up |
| `build.py` | builds the single-file `.pyz` |

**Releasing:** push a version tag. The GitHub Actions workflow then builds `cherry-picker.pyz`
and publishes it as a release with that name, which is what the install command downloads.
Ordinary pushes and merges don't release.

```bash
git switch main && git pull
git tag -a v0.2.0 -m "Cherry Picker 0.2.0"
git push origin v0.2.0
```
