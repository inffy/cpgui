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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "cherry-picker"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "cherry-picker"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cherry-picker" / "config.json"
COMMIT_LIMIT = 1000
DIFF_LIMIT = 300_000
PICKED_RE = re.compile(r"cherry picked from commit ([0-9a-f]{40})")
SEQ_HEAD = {"cherry-pick": "CHERRY_PICK_HEAD", "revert": "REVERT_HEAD"}
PR_SUBJECT_RE = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")
# (label, command) - only the ones installed are offered; the first one is the default
EDITORS = [("Zed", "zed"), ("Kate", "kate"), ("KWrite", "kwrite"), ("GNOME Text Editor", "gnome-text-editor"),
           ("Sublime Text", "subl"), ("gedit", "gedit"), ("VS Code", "code")]
SYSTEM_DEFAULT = "System default"

# --------------------------------------------------------------------------- git backend

class GitError(Exception):
    pass


class PushRejected(GitError):
    """The branch already exists on the remote with commits the local one doesn't have."""


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


def github_api(path):
    """GET from the GitHub REST API: through `gh` when logged in, else anonymously (public repos only)."""
    try:
        if gh_authenticated():
            p = subprocess.run(["gh", "api", path], capture_output=True, timeout=20)
            if p.returncode == 0:
                return json.loads(p.stdout)
            raise GitError(f"GitHub API {path}: {p.stderr.decode('utf-8', 'replace').strip()}")
        req = urllib.request.Request(f"https://api.github.com/{path}",
                                     headers={"Accept": "application/vnd.github+json", "User-Agent": "cherry-picker"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise GitError(f"GitHub API {path}: {e.code} {e.reason}") from e
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        raise GitError(f"Could not reach GitHub: {e}") from e


def parse_pr(text):
    """123, #123 or a .../pull/123 URL -> 123."""
    m = re.fullmatch(r"\s*(?:#?(\d+)|https?://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)\S*)\s*", text)
    if not m:
        raise GitError("Enter a pull request number (e.g. 123 or #123) or its GitHub URL.")
    return int(m.group(1) or m.group(2))


def revert_branch_name(branch, commits, now=None):
    """revert-<pr>[-<pr>...]-on-<branch>, else the short hashes, e.g. revert-123-on-main."""
    prs = [n for c in commits if (n := pr_from_subject(c["subject"]))]
    ids = list(dict.fromkeys(map(str, prs))) if prs else [c["short"] for c in commits[:3]]
    return f"revert-{'-'.join(ids) or f'{(now or datetime.datetime.now()):%Y%m%d-%H%M}'}-on-{branch}"


class Repo:
    def __init__(self, spec, log):
        self.spec = spec.strip()
        self.url, self.slug, name = parse_repo(spec)
        self.path = CACHE_DIR / "repos" / name
        self.log = log
        self.use_gh = self.url.startswith("https://github.com/") and gh_authenticated()
        self.pr_cache = {}

    # -- low level
    def run(self, args, check=True, in_repo=True, quiet=False, use_gh=None):
        cmd = ["git"]
        if self.use_gh if use_gh is None else use_gh:  # use the GitHub CLI login for https auth
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
            if args[0] == "push" and re.search(r"\[rejected\].*\((non-fast-forward|fetch first)\)", msg):
                raise PushRejected(f"git push was rejected:\n{msg}")
            raise GitError(f"git {args[0]} failed:\n{msg}")
        p.out, p.err = out, err
        return p

    def out(self, args, **kw):
        return self.run(args, quiet=True, **kw).out

    def in_pick(self, op="cherry-pick"):
        """True while a cherry-pick (or, with op="revert", a revert) is stopped."""
        return (self.path / ".git" / SEQ_HEAD[op]).exists()

    def in_merge(self):
        return (self.path / ".git" / "MERGE_HEAD").exists()

    def in_rebase(self):
        return any((self.path / ".git" / d).exists() for d in ("rebase-merge", "rebase-apply"))

    def abort_unfinished(self):
        """Drop a cherry-pick, revert, merge or rebase left half-done (e.g. the app was closed mid-conflict)."""
        for op in SEQ_HEAD:
            if self.in_pick(op):
                self.run([op, "--abort"], check=False)
        if self.in_merge():
            self.run(["merge", "--abort"], check=False)
        if self.in_rebase():
            self.run(["rebase", "--abort"], check=False)

    # -- repository
    def sync(self):
        if not (self.path / ".git").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["clone", "--no-tags", self.url, str(self.path)], in_repo=False)
        else:
            self.abort_unfinished()
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
    def new_branch(self, target, branch):
        """Fresh local branch `branch` at origin/<target> (recreated if it exists)."""
        if self.run(["check-ref-format", "--branch", branch], check=False, quiet=True).returncode:
            raise GitError(f"“{branch}” is not a valid branch name.")
        if self.run(["var", "GIT_COMMITTER_IDENT"], check=False, quiet=True).returncode:
            raise GitError("Git doesn't know who you are, so it can't make commits. Set your name and email:\n"
                           '  git config --global user.name "Your Name"\n'
                           "  git config --global user.email you@example.com")
        self.abort_unfinished()
        self.run(["checkout", "-f", "-B", branch, f"origin/{target}"])
        self.run(["clean", "-fdq"])

    def head(self):
        return self.out(["rev-parse", "HEAD"]).strip()

    def pick(self, sha, record_origin, merge, op="cherry-pick"):
        """Apply (op="cherry-pick") or undo (op="revert") one commit: "ok", "dropped" or "conflict"."""
        before = self.head()
        if op == "revert":
            args = ["revert", "--no-edit"]
        else:
            args = ["cherry-pick", "--allow-empty", "--empty=drop"]
            if record_origin:
                args.append("-x")
        if merge:
            args += ["-m", "1"]
        p = self.run(args + [sha], check=False)
        if p.returncode == 0:
            return "ok" if self.head() != before else "dropped"
        if self.in_pick(op):
            if self.conflicts():
                return "conflict"
            if self.nothing_staged():  # stopped because there is nothing to change (already applied / undone)
                self.run([op, "--skip"])
                return "dropped"
            self.run([op, "--abort"], check=False)  # e.g. the commit itself failed
        what = "Revert" if op == "revert" else "Cherry-pick"
        raise GitError(f"{what} of {sha[:10]} failed:\n{(p.err or p.out).strip()}")

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

    def stage_resolved(self, button="Continue"):
        """`git add` the conflicted files, refusing while any still has conflict markers."""
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
                           f"\n\nEdit those files, or settle every file one way with the buttons next to {button}.")
        if files:
            self.run(["add", "-A", "--"] + files)

    def continue_pick(self, op="cherry-pick"):
        self.stage_resolved()
        before = self.head()
        p = self.run([op, "--continue"], check=False)
        if p.returncode:
            if self.in_pick(op) and not self.conflicts() and self.nothing_staged():  # resolution left no changes
                self.run([op, "--skip"])
                return "dropped"
            raise GitError(f"{op} --continue failed:\n{(p.err or p.out).strip()}")
        return "ok" if self.head() != before else "dropped"

    def skip(self, op="cherry-pick"):
        self.run([op, "--skip"])

    def abort(self, op="cherry-pick"):
        if self.in_pick(op):
            self.run([op, "--abort"])

    # -- reverting
    def log_commits(self, *args):
        """`git log <args>` as commit dicts, newest first."""
        out = self.out(["log", f"--max-count={COMMIT_LIMIT}", "--date=short",
                        "--format=%H%x1f%h%x1f%an%x1f%ad%x1f%P%x1f%s", *args, "--"])
        result = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 6:
                sha, short, author, date, parents, subject = parts
                result.append(dict(sha=sha, short=short, author=author, date=date, subject=subject,
                                   merge=len(parents.split()) > 1, applied=False))
        return result

    def branch_commits(self, branch):
        """Changes that landed on origin/<branch>, newest first (first parent: one row per merged PR)."""
        return self.log_commits("--first-parent", f"origin/{branch}")

    # -- rebasing a pull request onto its base
    def pr_info(self, number):
        """The pull request from the GitHub API, plus its commits fetched into refs/pr/<number>."""
        if not self.slug:
            raise GitError("Rebasing a pull request needs a GitHub repository (owner/repo).")
        data = github_api(f"repos/{self.slug}/pulls/{number}")
        if not isinstance(data, dict) or "head" not in data:
            raise GitError(f"Could not find pull request #{number} in {self.slug}.")
        head_repo = data["head"].get("repo") or {}
        info = dict(number=number, title=data.get("title", ""), author=(data.get("user") or {}).get("login", "?"),
                    state="merged" if data.get("merged_at") else data.get("state", "open"),
                    draft=bool(data.get("draft")), base=data["base"]["ref"], head=data["head"]["ref"],
                    head_sha=data["head"]["sha"], head_repo=head_repo.get("full_name"),
                    head_ssh=head_repo.get("ssh_url"), head_https=head_repo.get("clone_url"),
                    can_modify=bool(data.get("maintainer_can_modify")),
                    url=data.get("html_url") or f"https://github.com/{self.slug}/pull/{number}")
        info["same_repo"] = (info["head_repo"] or "").lower() == self.slug.lower()
        self.fetch()
        self.run(["fetch", "--no-tags", "origin", f"+refs/pull/{number}/head:refs/pr/{number}"])
        info["fetched_sha"] = self.out(["rev-parse", f"refs/pr/{number}"]).strip()
        rng = f"origin/{info['base']}..refs/pr/{number}"
        info["commits"] = self.log_commits("--topo-order", rng)
        info["behind"] = int(self.out(["rev-list", "--count", f"refs/pr/{number}..origin/{info['base']}"]).strip())
        return info

    def start_rebase(self, info):
        """Check out the PR head and rebase it onto origin/<base>: "ok", "uptodate" or "conflict"."""
        self.abort_unfinished()
        branch = f"pr-{info['number']}"
        self.run(["checkout", "-f", "-B", branch, f"refs/pr/{info['number']}"])
        self.run(["clean", "-fdq"])
        base = f"origin/{info['base']}"
        if self.run(["merge-base", "--is-ancestor", base, "HEAD"], check=False, quiet=True).returncode == 0:
            return "uptodate"
        p = self.run(["rebase", base], check=False)
        return self._rebase_state(p)

    def _rebase_state(self, p):
        if p.returncode == 0 and not self.in_rebase():
            return "ok"
        if self.in_rebase() and self.conflicts():
            return "conflict"
        if self.in_rebase():
            self.run(["rebase", "--abort"], check=False)
        raise GitError(f"git rebase failed:\n{(p.err or p.out).strip()}")

    def rebase_step(self):
        """(position, total, short sha, subject) of the commit the rebase stopped on."""
        d = self.path / ".git" / "rebase-merge"
        try:
            pos, total = int((d / "msgnum").read_text()), int((d / "end").read_text())
        except (OSError, ValueError):
            pos = total = 0
        p = self.run(["log", "-1", "--format=%h%x1f%s", "REBASE_HEAD"], check=False, quiet=True)
        short, _, subject = p.out.strip().partition("\x1f")
        return pos, total, short, subject

    def rebase_continue(self):
        self.stage_resolved()
        if not self.conflicts() and self.nothing_staged() and self.in_rebase():
            p = self.run(["rebase", "--skip"], check=False)  # resolution left no changes: drop the commit
        else:
            p = self.run(["rebase", "--continue"], check=False)
        return self._rebase_state(p)

    def rebase_skip(self):
        return self._rebase_state(self.run(["rebase", "--skip"], check=False))

    def rebase_abort(self):
        if self.in_rebase():
            self.run(["rebase", "--abort"])

    def push_pr(self, info):
        """Force-push the rebased branch to the PR's head branch, only if it still is what we fetched."""
        if info["same_repo"]:
            remote, use_gh = "origin", None
        else:
            if not info["head_repo"]:
                raise GitError("The pull request's fork no longer exists, so it can't be pushed to.")
            remote = info["head_ssh"] if self.url.startswith("git@") else info["head_https"]
            use_gh = remote.startswith("https://github.com/") and gh_authenticated()
        ref = f"refs/heads/{info['head']}"
        p = self.run(["push", f"--force-with-lease={ref}:{info['fetched_sha']}", remote, f"HEAD:{ref}"],
                     check=False, use_gh=use_gh)
        if p.returncode:
            msg = (p.err or p.out).strip()
            if "stale info" in msg or "fetch first" in msg:
                raise GitError(f"Nothing was pushed: pull request #{info['number']} changed on GitHub after you "
                               "looked it up. Look it up again and redo the rebase.\n\n" + msg)
            raise GitError(f"git push failed:\n{msg}")

    def push(self, branch, fork=None, replace=False):
        """Push to this repository, or to `fork` (owner/repo or a git URL) through a `fork` remote.

        Raises PushRejected when the branch already exists there with other commits. With replace=True
        it is overwritten, but only if it still points where it did a moment ago (--force-with-lease).
        """
        remote, use_gh = "origin", None
        if fork:
            url, _slug, _name = parse_repo(fork)
            if self.run(["remote", "get-url", "fork"], check=False, quiet=True).returncode:
                self.run(["remote", "add", "fork", url])
            else:
                self.run(["remote", "set-url", "fork", url])
            remote, use_gh = "fork", url.startswith("https://github.com/") and gh_authenticated()
        args = ["push", "-u"]
        if replace:
            ref = f"refs/heads/{branch}"
            old = self.out(["ls-remote", remote, ref], use_gh=use_gh).split()
            args.append(f"--force-with-lease={ref}:{old[0]}" if old else f"--force-with-lease={ref}:")
        self.run(args + [remote, f"{branch}:{branch}"], use_gh=use_gh)

    # -- promoting (merge a whole branch into another)
    def promote_commits(self, target, source):
        """Commits a merge of origin/<source> would bring into origin/<target>, newest first."""
        result = self.log_commits("--topo-order", f"origin/{target}..origin/{source}")
        total = int(self.out(["rev-list", "--count", f"origin/{target}..origin/{source}", "--"]).strip() or 0)
        stat = self.out(["diff", "--shortstat", f"origin/{target}...origin/{source}", "--"]).strip()
        return result, total, stat

    def fetch(self):
        self.run(["fetch", "--prune", "--no-tags", "origin"])

    def merge(self, source):
        """Merge origin/<source> into the current branch: "ok", "uptodate" or "conflict"."""
        before = self.head()
        p = self.run(["merge", "--no-edit", f"origin/{source}"], check=False)
        if p.returncode == 0:
            return "ok" if self.head() != before else "uptodate"
        if self.in_merge() and self.conflicts():
            return "conflict"
        if self.in_merge():
            self.run(["merge", "--abort"], check=False)
        raise GitError(f"Merging origin/{source} failed:\n{(p.err or p.out).strip()}")

    def commit_merge(self):
        self.stage_resolved("Commit Merge")
        self.run(["commit", "--no-edit"])

    def abort_merge(self):
        if self.in_merge():
            self.run(["merge", "--abort"])

    def pr_number(self, commit):
        """PR number that introduced this commit: from the subject, else asked from GitHub."""
        n = pr_from_subject(commit["subject"])
        if n or not self.slug:
            return n
        sha = commit["sha"]
        if sha in self.pr_cache:
            return self.pr_cache[sha]
        try:
            data = github_api(f"repos/{self.slug}/commits/{sha}/pulls")
        except GitError:
            return None  # don't cache failures; maybe offline
        prs = data if isinstance(data, list) else []
        merged = [p for p in prs if p.get("merged_at")]
        n = (merged or prs or [{}])[0].get("number")
        self.pr_cache[sha] = n
        return n

    def compare_url(self, target, branch, fork=None):
        """GitHub page to open a PR of `branch` (on this repo, or on the fork) into `target`."""
        q = urllib.parse.quote
        head = q(branch, safe="")
        if fork:
            fork_slug = parse_repo(fork)[1]
            if not fork_slug:
                return None
            head = f"{q(fork_slug.split('/')[0], safe='')}:{head}"
        elif target == branch:
            return None
        if not self.slug:
            return None
        return f"https://github.com/{self.slug}/compare/{q(target, safe='')}...{head}?expand=1"


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


def promote_branch_name(source, target):
    return f"promote-{source}-to-{target}"


def guess_promote_target(branches, default):
    """Newest branch that looks like a stable/release branch, else the newest other branch."""
    others = [b for b in branches if b != default]
    for b in others:  # `branches` is newest first
        if re.search(r"stable|release", b, re.I):
            return b
    return others[0] if others else default


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
