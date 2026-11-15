"""Git / GitHub backend and settings (no GUI imports here)."""
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "cherry-picker"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cherry-picker"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cherry-picker" / "config.json"
COMMIT_LIMIT = 1000
DIFF_LIMIT = 300_000
PICKED_RE = re.compile(r"cherry picked from commit ([0-9a-f]{40})")
PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")
# (label, command) - only the ones installed are offered; the first one is the default
EDITORS = [("Zed", "zed"), ("Kate", "kate"), ("KWrite", "kwrite"), ("GNOME Text Editor", "gnome-text-editor"),
           ("Sublime Text", "subl"), ("gedit", "gedit"), ("VS Code", "code")]
SYSTEM_DEFAULT = "System default"

# --------------------------------------------------------------------------- git backend

class GitError(Exception):
    pass


def parse_repo(text):
    """Return (clone_url, github_slug_or_None, cache_dir_name)."""
    text = text.strip()
    m = re.match(r"^(?:https?://github\.com/|git@github\.com:)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", text)
    looks_remote = text.startswith(("http", "git@"))
    if m and (looks_remote or not os.path.exists(text)):
        owner, name = m.groups()
        slug = f"{owner}/{name}"
        url = text if text.startswith("git@") else f"https://github.com/{slug}.git"
        return url, slug, f"{owner}__{name}"
    if not text:
        raise GitError("Enter a repository first (e.g. owner/repo).")
    return text, None, re.sub(r"[^\w.-]+", "_", text.strip("/"))[-80:]


def pr_from_subject(subject):
    """`Fix thing (#123)` (squash merge) or `Merge pull request #123 from ...`."""
    m = PR_SUBJECT_RE.search(subject)
    return int(m.group(1) or m.group(2)) if m else None


_gh_ok = None


def gh_authenticated():
    global _gh_ok
    if _gh_ok is None:
        try:
            _gh_ok = subprocess.run(["gh", "auth", "status", "--hostname", "github.com"],
                                    capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            _gh_ok = False
    return _gh_ok


class Repo:
    def __init__(self, spec, log):
        self.spec = spec.strip()
        self.url, self.slug, name = parse_repo(spec)
        self.path = CACHE_DIR / "repos" / name
        self.log = log
        self.use_gh = self.url.startswith("https://github.com/") and gh_authenticated()
        self.pr_cache = {}

    # -- low level
    def run(self, args, check=True, in_repo=True, quiet=False):
        cmd = ["git"]
        if self.use_gh:  # use the GitHub CLI login for https auth
            cmd += ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
        cmd += args
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "LC_ALL": "C", "LANG": "C"}
        if not quiet:
            self.log("$ " + shlex.join(["git"] + args))
        p = subprocess.run(cmd, cwd=self.path if in_repo else None, capture_output=True, env=env)
        out = p.stdout.decode("utf-8", "replace")
        err = p.stderr.decode("utf-8", "replace")
        if not quiet:
            for text in (out, err):
                if text.strip():
                    self.log(text.rstrip())
        if check and p.returncode:
            msg = (err or out).strip()
            if "could not read Username" in msg or "Authentication failed" in msg:
                msg += ("\n\nThis repository needs authentication. Run `gh auth login` in a terminal "
                        "(or set up SSH keys and use the git@github.com:owner/repo URL).")
            raise GitError(f"git {args[0]} failed:\n{msg}")
        p.out, p.err = out, err
        return p

    def out(self, args, **kw):
        return self.run(args, quiet=True, **kw).out

    def in_pick(self):
        return (self.path / ".git" / "CHERRY_PICK_HEAD").exists()

    # -- repository
    def sync(self):
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["clone", "--no-tags", self.url, str(self.path)], in_repo=False)
        else:
            if self.in_pick():
                self.run(["cherry-pick", "--abort"], check=False)
            self.run(["remote", "set-url", "origin", self.url])
            self.run(["fetch", "--prune", "--no-tags", "origin"])
        self.run(["remote", "set-head", "origin", "--auto"], check=False, quiet=True)
        branches = [b for b in self.out(["for-each-ref", "--sort=-committerdate",
                                         "--format=%(refname:lstrip=3)", "refs/remotes/origin/"]).split()
                    if b != "HEAD"]
        head = self.run(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], check=False, quiet=True)
        default = head.out.strip().removeprefix("origin/") if head.returncode == 0 else None
        return branches, default

    def commits(self, target, source, merges):
        args = ["log", "--topo-order", "--cherry-mark", "--right-only", f"--max-count={COMMIT_LIMIT}",
                "--date=short", "--format=%m%x1f%H%x1f%h%x1f%an%x1f%ad%x1f%P%x1f%s",
                f"origin/{target}...origin/{source}", "--"]
        if not merges:
            args.insert(1, "--no-merges")
        # commits already brought over with `cherry-pick -x` (patch may differ)
        picked = set(PICKED_RE.findall(self.out(
            ["log", "--format=%B", "--grep=cherry picked from commit",
             f"origin/{source}..origin/{target}", "--"])))
        result = []
        for line in self.out(args).splitlines():
            parts = line.split("\x1f")
            if len(parts) != 7:
                continue
            mark, sha, short, author, date, parents, subject = parts
            result.append(dict(sha=sha, short=short, author=author, date=date, subject=subject,
                               merge=len(parents.split()) > 1,
                               applied=mark == "=" or sha in picked))
        return result

    def show(self, sha):
        text = self.out(["show", "--stat", "--patch", "--format=fuller", "--no-color", sha])
        if len(text) > DIFF_LIMIT:
            text = text[:DIFF_LIMIT] + "\n\n... (diff truncated)\n"
        return text

    # -- cherry-picking
    def start_branch(self, target, branch):
        if self.run(["check-ref-format", "--branch", branch], check=False, quiet=True).returncode:
            raise GitError(f"“{branch}” is not a valid branch name.")
        if self.run(["var", "GIT_COMMITTER_IDENT"], check=False, quiet=True).returncode:
            raise GitError("Git doesn't know who you are, so it can't make commits. Set your name and email:\n"
                           '  git config --global user.name "Your Name"\n'
                           "  git config --global user.email you@example.com")
        if self.in_pick():
            self.run(["cherry-pick", "--abort"], check=False)
        self.run(["checkout", "-f", "-B", branch, f"origin/{target}"])
        self.run(["clean", "-fdq"])

    def head(self):
        return self.out(["rev-parse", "HEAD"]).strip()

    def pick(self, sha, record_origin, merge):
        before = self.head()
        args = ["cherry-pick", "--allow-empty", "--empty=drop"]
        if record_origin:
            args.append("-x")
        if merge:
            args += ["-m", "1"]
        p = self.run(args + [sha], check=False)
        if p.returncode == 0:
            return "ok" if self.head() != before else "dropped"
        if self.in_pick():
            if self.conflicts():
                return "conflict"
            if self.nothing_staged():  # stopped because the changes are already on the branch
                self.run(["cherry-pick", "--skip"])
                return "dropped"
            self.run(["cherry-pick", "--abort"], check=False)  # e.g. the commit itself failed
        raise GitError(f"Cherry-pick of {sha[:10]} failed:\n{(p.err or p.out).strip()}")

    def nothing_staged(self):
        return self.run(["diff", "--cached", "--quiet"], check=False, quiet=True).returncode == 0

    def conflicts(self):
        return [f for f in self.out(["diff", "--name-only", "--diff-filter=U", "-z"]).split("\0") if f]

    def resolve_all(self, side):
        for f in self.conflicts():
            if self.run(["checkout", f"--{side}", "--", f], check=False).returncode:
                self.run(["rm", "--quiet", "--", f], check=False)  # file deleted on that side
            else:
                self.run(["add", "--", f])

    def continue_pick(self):
        files = self.conflicts()
        marked = []
        for f in files:
            p = self.path / f
            if p.is_file():
                data = p.read_bytes()
                if b"<<<<<<< " in data and b">>>>>>> " in data:
                    marked.append(f)
        if marked:
            raise GitError("Conflict markers are still present in:\n  " + "\n  ".join(marked) +
                           "\n\nEdit those files (or use Take incoming / Keep target), then press Continue.")
        if files:
            self.run(["add", "-A", "--"] + files)
        before = self.out(["rev-parse", "HEAD"]).strip()
        p = self.run(["cherry-pick", "--continue"], check=False)
        if p.returncode:
            if self.in_pick() and not self.conflicts() and self.nothing_staged():  # resolution left no changes
                self.run(["cherry-pick", "--skip"])
                return "dropped"
            raise GitError(f"cherry-pick --continue failed:\n{(p.err or p.out).strip()}")
        return "ok" if self.head() != before else "dropped"

    def skip(self):
        self.run(["cherry-pick", "--skip"])

    def abort(self):
        if self.in_pick():
            self.run(["cherry-pick", "--abort"])

    def push(self, branch):
        self.run(["push", "-u", "origin", f"{branch}:{branch}"])

    def pr_number(self, commit):
        """PR number that introduced this commit: from the subject, else asked from GitHub."""
        n = pr_from_subject(commit["subject"])
        if n or not self.slug:
            return n
        sha = commit["sha"]
        if sha in self.pr_cache:
            return self.pr_cache[sha]
        path = f"repos/{self.slug}/commits/{sha}/pulls"
        data = None
        try:
            if gh_authenticated():
                p = subprocess.run(["gh", "api", path], capture_output=True, timeout=20)
                if p.returncode == 0:
                    data = json.loads(p.stdout)
            else:  # public repos work without auth (rate limited)
                req = urllib.request.Request(f"https://api.github.com/{path}",
                                             headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": "cherry-picker"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = json.loads(r.read())
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None  # don't cache failures; maybe offline
        prs = data if isinstance(data, list) else []
        merged = [p for p in prs if p.get("merged_at")]
        n = (merged or prs or [{}])[0].get("number")
        self.pr_cache[sha] = n
        return n

    def compare_url(self, target, branch):
        if not self.slug or target == branch:
            return None
        q = urllib.parse.quote
        return f"https://github.com/{self.slug}/compare/{q(target, safe='')}...{q(branch, safe='')}?expand=1"


# --------------------------------------------------------------------------- config

def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except OSError:
        pass


def branch_name(target, prs, now=None):
    """<pr>[-<pr>...]-to-<target>, e.g. 123-to-stable; date-based when no PR is known."""
    if prs:
        return "-".join(map(str, prs)) + f"-to-{target}"
    return f"cherry-pick-{(now or datetime.datetime.now()):%Y%m%d-%H%M}-to-{target}"


def _search_path():
    # apps started from the Plasma menu may not have ~/.local/bin (where Zed installs) on PATH
    extra = [str(Path.home() / ".local" / "bin")]
    return os.pathsep.join([os.environ.get("PATH", "")] + extra)


def installed_editors():
    return [(n, c) for n, c in EDITORS if shutil.which(c, path=_search_path())] + [(SYSTEM_DEFAULT, None)]


def editor_icon_path(cmd):
    """Icon file from the editor's .desktop entry when it uses a path instead of a theme name (e.g. Zed)."""
    dirs = [Path.home() / ".local/share/applications", Path("/usr/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications"]
    for d in dirs:
        for desktop in sorted(d.glob(f"*{cmd}*.desktop")):
            try:
                m = re.search(r"^Icon=(/.+)$", desktop.read_text(errors="replace"), re.M)
            except OSError:
                continue
            if m and Path(m.group(1)).is_file():
                return m.group(1)
    return None


def first_conflict_line(path):
    try:
        with open(path, "rb") as f:
            for n, line in enumerate(f, 1):
                if line.startswith(b"<<<<<<< "):
                    return n
    except OSError:
        pass
    return None


def editor_args(cmd, repo_path, files):
    """Command line to open the conflicted files.

    Zed and VS Code also get the clone as their project, so their git panels / merge editors
    see the conflicts; Zed jumps to the first conflict marker of each file (path:line).
    """
    exe = shutil.which(cmd, path=_search_path()) or cmd
    if cmd == "zed":
        return [exe, str(repo_path)] + [f"{f}:{n}" if (n := first_conflict_line(f)) else f for f in files]
    if cmd == "code":
        return [exe, str(repo_path)] + files
    return [exe] + (files or [str(repo_path)])


def launch(args, cwd):
    subprocess.Popen(args, cwd=cwd, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
