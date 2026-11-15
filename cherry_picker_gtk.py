#!/usr/bin/python3
"""Cherry Picker - the GTK4 version of an app for cherry-picking commits between branches of a GitHub repo.

Usage:  python3 cherry_picker_gtk.py [owner/repo | URL]

The repository is cloned into ~/.cache/cherry-picker/repos/ and all work happens
in that clone, so your own checkouts are never touched.
"""
import os
import re
import shlex
import sys
import threading
from pathlib import Path

try:
    import gi
except ModuleNotFoundError:
    # GTK bindings come with the distro's Python; e.g. a Homebrew python3 earlier in PATH lacks them.
    SYSTEM_PY = "/usr/bin/python3"
    if os.path.realpath(sys.executable) != os.path.realpath(SYSTEM_PY) and os.path.exists(SYSTEM_PY):
        os.execv(SYSTEM_PY, [SYSTEM_PY, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit("PyGObject (gi) is missing. Install it with: sudo dnf install python3-gobject gtk4")

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "io.github.cherrypicker.CherryPicker"

from cpcore import *  # noqa: E402,F403
from cpcore import Repo, GitError, branch_name, editor_args, installed_editors, launch  # noqa: E402

CSS = """
.sha { font-family: monospace; opacity: 0.7; }
.meta { font-size: smaller; opacity: 0.7; }
.badge { font-size: smaller; padding: 1px 8px; border-radius: 9px;
         background: alpha(currentColor, 0.12); }
.applied { opacity: 0.55; }
.bar { padding: 10px; border-radius: 8px; }
.conflict-bar { background: alpha(#e5a50a, 0.22); }
.result-bar { background: alpha(#26a269, 0.20); }
.section-title { font-weight: bold; }
"""


# --------------------------------------------------------------------------- UI

def hbox(spacing=6, **kw):
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing, **kw)


def vbox(spacing=6, **kw):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kw)


def label(text, *classes, **kw):
    kw.setdefault("xalign", 0)
    lbl = Gtk.Label(label=text, **kw)
    for c in classes:
        lbl.add_css_class(c)
    return lbl


def button(text, cb, *classes, tooltip=None, icon=None):
    b = Gtk.Button(icon_name=icon) if icon else Gtk.Button(label=text)
    if tooltip:
        b.set_tooltip_text(tooltip)
    for c in classes:
        b.add_css_class(c)
    b.connect("clicked", lambda *_: cb())
    return b


class CommitRow(Gtk.ListBoxRow):
    def __init__(self, commit, on_toggle):
        super().__init__()
        self.commit = commit
        box = hbox(10, margin_top=5, margin_bottom=5, margin_start=6, margin_end=8)
        self.check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self.check.connect("toggled", lambda *_: on_toggle())
        box.append(self.check)
        box.append(label(commit["short"], "sha", valign=Gtk.Align.CENTER))
        text = vbox(2, hexpand=True)
        subj = label(commit["subject"], ellipsize=Pango.EllipsizeMode.END)
        subj.set_tooltip_text(commit["subject"])
        text.append(subj)
        meta = f"{commit['author']} · {commit['date']}" + (" · merge commit" if commit["merge"] else "")
        text.append(label(meta, "meta", ellipsize=Pango.EllipsizeMode.END))
        box.append(text)
        pr = pr_from_subject(commit["subject"])
        if pr:
            box.append(label(f"#{pr}", "badge", valign=Gtk.Align.CENTER))
        if commit["applied"]:
            box.append(label("already in target", "badge", valign=Gtk.Align.CENTER))
            text.add_css_class("applied")
        self.set_child(box)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, initial_repo=None):
        super().__init__(application=app, title="Cherry Picker", default_width=1250, default_height=820)
        self.cfg = load_config()
        self.repo = None
        self.rows = []
        self.busy = 0
        self.picking = False
        self.suppress = False
        self.branch_auto = True
        self.diff_req = 0
        self.pr_req = 0
        self.pr_timer = 0
        self.suggested_prs = []
        self.conflict_files = []
        self.queue, self.qi, self.stats = [], 0, {}
        self.last_branch = self.last_target = None

        header = Gtk.HeaderBar()
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        self.set_titlebar(header)

        root = vbox(8, margin_top=10, margin_bottom=10, margin_start=12, margin_end=12)
        self.set_child(root)

        # repository row
        row = hbox()
        row.append(label("Repository", "section-title"))
        self.repo_entry = Gtk.Entry(hexpand=True, placeholder_text="owner/repo, a GitHub URL, or any git URL")
        self.repo_entry.connect("activate", lambda *_: self.on_load())
        row.append(self.repo_entry)
        self.recent_btn = Gtk.MenuButton(icon_name="document-open-recent-symbolic", tooltip_text="Recent repositories")
        row.append(self.recent_btn)
        self.load_btn = button("Load", self.on_load, "suggested-action")
        row.append(self.load_btn)
        root.append(row)
        self.rebuild_recent()

        # branch row
        row = hbox(8)
        row.append(label("Pick from", "section-title"))
        self.src_dd = self.make_dropdown()
        row.append(self.src_dd)
        self.swap_btn = button("", self.on_swap, icon="object-flip-horizontal-symbolic", tooltip="Swap branches")
        row.append(self.swap_btn)
        row.append(label("onto", "section-title"))
        self.dst_dd = self.make_dropdown()
        row.append(self.dst_dd)
        self.refresh_btn = button("", self.on_refresh, icon="view-refresh-symbolic", tooltip="Fetch latest from remote")
        row.append(self.refresh_btn)
        row.append(Gtk.Box(hexpand=True))
        self.merges_cb = Gtk.CheckButton(label="Include merge commits")
        self.merges_cb.connect("toggled", lambda *_: self.load_commits())
        row.append(self.merges_cb)
        self.hide_cb = Gtk.CheckButton(label="Hide commits already in target", active=True)
        self.hide_cb.connect("toggled", lambda *_: (self.listbox.invalidate_filter(), self.update_count()))
        row.append(self.hide_cb)
        root.append(row)

        # main split: commits | diff
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, position=600)
        left = vbox()
        tools = hbox()
        self.search = Gtk.SearchEntry(hexpand=True, placeholder_text="Filter by message, author or hash")
        self.search.connect("search-changed", lambda *_: self.listbox.invalidate_filter())
        tools.append(self.search)
        tools.append(button("All", lambda: self.select_visible(True), tooltip="Select all visible commits"))
        tools.append(button("None", lambda: self.select_visible(False), tooltip="Clear selection"))
        left.append(tools)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.set_filter_func(self.row_visible)
        self.listbox.connect("row-selected", self.on_row_selected)
        self.placeholder = label("Load a repository to see its commits.", "meta", xalign=0.5,
                                 margin_top=40, margin_bottom=40, wrap=True)
        self.listbox.set_placeholder(self.placeholder)
        left.append(Gtk.ScrolledWindow(vexpand=True, child=self.listbox, has_frame=True))
        self.count_lbl = label("", "meta")
        left.append(self.count_lbl)
        paned.set_start_child(left)
        paned.set_shrink_start_child(False)

        self.diff = Gtk.TextView(editable=False, monospace=True, cursor_visible=False,
                                 left_margin=8, top_margin=6, right_margin=8)
        buf = self.diff.get_buffer()
        buf.create_tag("add", foreground="#2ea043")
        buf.create_tag("del", foreground="#e5484d")
        buf.create_tag("hunk", foreground="#3b82f6")
        buf.create_tag("file", weight=Pango.Weight.BOLD)
        buf.create_tag("head", weight=Pango.Weight.BOLD)
        paned.set_end_child(Gtk.ScrolledWindow(child=self.diff, has_frame=True, margin_start=6))
        root.append(paned)

        # conflict bar
        self.conflict_bar = vbox(8, visible=False)
        self.conflict_bar.add_css_class("bar")
        self.conflict_bar.add_css_class("conflict-bar")
        self.conflict_lbl = label("", wrap=True, selectable=True)
        self.conflict_bar.append(self.conflict_lbl)
        row = hbox()
        row.append(button("Open in editor", self.open_conflicts_in_editor, "suggested-action",
                          tooltip="Open the conflicted files in the chosen editor"))
        self.editors = installed_editors()
        names = [n for n, _ in self.editors]
        self.editor_dd = Gtk.DropDown.new_from_strings(names)
        self.editor_dd.set_tooltip_text("Editor used for conflicts")
        saved = self.cfg.get("editor")
        self.editor_dd.set_selected(names.index(saved) if saved in names else 0)
        self.editor_dd.connect("notify::selected", lambda *_: self.save_editor_prefs())
        row.append(self.editor_dd)
        row.append(button("Open folder", self.open_folder, tooltip="Open the working copy in the file manager"))
        row.append(button("Take incoming", lambda: self.on_resolve("theirs"),
                          tooltip="Resolve every conflicted file with the version from the picked commit"))
        row.append(button("Keep target", lambda: self.on_resolve("ours"),
                          tooltip="Resolve every conflicted file with the target branch version"))
        row.append(Gtk.Box(hexpand=True))
        row.append(button("Skip this commit", self.on_skip))
        row.append(button("Stop", self.on_stop, "destructive-action",
                          tooltip="Abort this commit and stop; earlier picks stay on the branch"))
        row.append(button("Continue", self.on_continue, "suggested-action",
                          tooltip="I've resolved the conflicts - commit and go on"))
        self.conflict_bar.append(row)
        self.conflict_btns = [c for c in iter_children(row) if isinstance(c, Gtk.Button)]
        root.append(self.conflict_bar)

        # result bar
        self.result_bar = hbox(8, visible=False)
        self.result_bar.add_css_class("bar")
        self.result_bar.add_css_class("result-bar")
        self.result_lbl = label("", wrap=True, hexpand=True, selectable=True)
        self.result_bar.append(self.result_lbl)
        self.push_btn = button("Push", self.on_push, "suggested-action")
        self.pr_btn = button("Open pull request", self.on_open_pr, "suggested-action")
        self.result_bar.append(button("Open folder", self.open_folder))
        self.result_bar.append(self.push_btn)
        self.result_bar.append(self.pr_btn)
        self.result_bar.append(button("", lambda: self.result_bar.set_visible(False),
                                      icon="window-close-symbolic", tooltip="Dismiss"))
        root.append(self.result_bar)

        # options row
        row = hbox(10)
        row.append(label("New branch", "section-title"))
        self.branch_entry = Gtk.Entry(width_chars=34, tooltip_text=(
            "Branch to create from the target and put the picks on. "
            "Use the target branch's own name to commit straight onto it."))
        self.branch_entry.connect("changed", self.on_branch_edited)
        row.append(self.branch_entry)
        self.x_cb = Gtk.CheckButton(label="Record origin (-x)", active=self.cfg.get("record_origin", True),
                                    tooltip_text="Append “(cherry picked from commit …)” to each message")
        row.append(self.x_cb)
        self.push_cb = Gtk.CheckButton(label="Push when done", active=self.cfg.get("push", False))
        row.append(self.push_cb)
        self.auto_editor_cb = Gtk.CheckButton(label="Open editor on conflict",
                                              active=self.cfg.get("auto_editor", False))
        self.auto_editor_cb.connect("toggled", lambda *_: self.save_editor_prefs())
        row.append(self.auto_editor_cb)
        row.append(Gtk.Box(hexpand=True))
        self.status = label("", "meta", ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        row.append(self.status)
        self.go_btn = button("Cherry-pick", self.on_go, "suggested-action")
        row.append(self.go_btn)
        root.append(row)

        # log
        exp = Gtk.Expander(label="Git output")
        self.logview = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, left_margin=6)
        exp.set_child(Gtk.ScrolledWindow(child=self.logview, min_content_height=160, has_frame=True))
        root.append(exp)

        self.controls = [self.repo_entry, self.recent_btn, self.load_btn, self.src_dd, self.dst_dd,
                         self.swap_btn, self.refresh_btn, self.merges_cb, self.branch_entry]
        self.update_sensitivity()
        self.update_count()

        recent = self.cfg.get("recent", [])
        if initial_repo:
            self.repo_entry.set_text(initial_repo)
            GLib.idle_add(self.on_load)
        elif recent:
            self.repo_entry.set_text(recent[0])

    # ---------- helpers
    def make_dropdown(self):
        dd = Gtk.DropDown.new(Gtk.StringList.new([]), None)
        dd.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        dd.set_enable_search(True)
        dd.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
        dd.set_size_request(220, -1)
        dd.connect("notify::selected", self.on_branch_changed)
        return dd

    @staticmethod
    def dd_value(dd):
        item = dd.get_selected_item()
        return item.get_string() if item else None

    @staticmethod
    def dd_select(dd, value):
        model = dd.get_model()
        for i in range(model.get_n_items()):
            if model.get_string(i) == value:
                dd.set_selected(i)
                return True
        return False

    def log(self, text):
        GLib.idle_add(self._append_log, text)

    def _append_log(self, text):
        buf = self.logview.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        self.logview.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
        buf.place_cursor(buf.get_end_iter())
        return False

    def set_status(self, text):
        self.status.set_text(text)

    def error(self, exc):
        self.log(f"ERROR: {exc}")
        dlg = Gtk.AlertDialog(message="Something went wrong", detail=str(exc))
        dlg.show(self)

    def run_bg(self, fn, done=None, fail=None):
        self.busy += 1
        self.update_sensitivity()

        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(finish, None, e)
            else:
                GLib.idle_add(finish, res, None)

        def finish(res, exc):
            self.busy -= 1
            self.update_sensitivity()
            if exc is not None:
                (fail or self.error)(exc)
            elif done:
                done(res)
            return False

        threading.Thread(target=work, daemon=True).start()

    def update_sensitivity(self):
        idle = self.busy == 0 and not self.picking
        for w in self.controls:
            w.set_sensitive(idle)
        self.go_btn.set_sensitive(idle and self.repo is not None and self.selected_count() > 0)
        for b in getattr(self, "conflict_btns", []):
            b.set_sensitive(self.busy == 0)
        self.push_btn.set_sensitive(idle)
        if self.busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def selected_commits(self):
        return [r.commit for r in self.rows if r.check.get_active()]

    def selected_count(self):
        return sum(r.check.get_active() for r in self.rows)

    def update_count(self):
        n = self.selected_count()
        shown = sum(self.row_visible(r) for r in self.rows)
        hidden = len(self.rows) - shown
        txt = f"{len(self.rows)} commits" if self.rows else ""
        if hidden:
            txt += f" ({hidden} hidden)"
        if len(self.rows) >= COMMIT_LIMIT:
            txt += f" - showing newest {COMMIT_LIMIT}"
        if n:
            txt += f" · {n} selected"
        self.count_lbl.set_text(txt)
        self.go_btn.set_label(f"Cherry-pick {n} commit{'s' if n != 1 else ''}" if n else "Cherry-pick")
        self.update_sensitivity()

    def row_visible(self, row):
        c = row.commit
        if self.hide_cb.get_active() and c["applied"]:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in c["subject"].lower() or q in c["author"].lower() or c["sha"].startswith(q)

    def select_visible(self, on):
        for r in self.rows:
            if not on:
                r.check.set_active(False)
            elif self.row_visible(r):
                r.check.set_active(True)

    def rebuild_recent(self):
        pop = Gtk.Popover()
        box = vbox(2, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
        recent = self.cfg.get("recent", [])
        for spec in recent:
            b = Gtk.Button(label=spec, has_frame=False)
            b.connect("clicked", lambda _b, s=spec: (pop.popdown(), self.repo_entry.set_text(s), self.on_load()))
            box.append(b)
        if not recent:
            box.append(label("No recent repositories", "meta", margin_start=8, margin_end=8))
        pop.set_child(box)
        self.recent_btn.set_popover(pop)

    def save_editor_prefs(self):
        self.cfg["editor"] = self.editors[self.editor_dd.get_selected()][0]
        self.cfg["auto_editor"] = self.auto_editor_cb.get_active()
        save_config(self.cfg)

    def open_conflicts_in_editor(self):
        if not self.repo:
            return
        files = [str(self.repo.path / f) for f in self.conflict_files if (self.repo.path / f).is_file()]
        name, cmd = self.editors[self.editor_dd.get_selected()]
        try:
            if cmd is None:
                for f in files:
                    Gio.AppInfo.launch_default_for_uri(Path(f).as_uri(), None)
            else:
                # VS Code: open the clone as the workspace too, so its merge editor / git view work
                args = editor_args(cmd, self.repo.path, files)
                self.log("$ " + shlex.join(args))
                launch(args, self.repo.path)
        except (OSError, GLib.Error) as e:
            self.error(GitError(f"Could not start {name}: {e}"))

    def open_folder(self):
        if self.repo:
            Gio.AppInfo.launch_default_for_uri(self.repo.path.as_uri(), None)

    def default_branch_name(self):
        return branch_name(self.dd_value(self.dst_dd) or "target", self.suggested_prs)

    def on_selection_changed(self):
        self.update_count()
        if self.pr_timer:  # debounce: "All" toggles many rows at once
            GLib.source_remove(self.pr_timer)
        self.pr_timer = GLib.timeout_add(250, self.lookup_prs)

    def lookup_prs(self):
        """Find PR numbers of the selected commits (oldest first) for the branch name."""
        self.pr_timer = 0
        self.pr_req += 1
        req, repo = self.pr_req, self.repo
        commits = list(reversed(self.selected_commits()))
        if not repo or not commits:
            self.suggested_prs = []
            self.set_branch_suggestion()
            return False

        def work():
            prs = []
            for c in commits:
                n = repo.pr_number(c)
                if n and n not in prs:
                    prs.append(n)
            GLib.idle_add(done, prs)

        def done(prs):
            if req == self.pr_req:
                self.suggested_prs = prs
                self.set_branch_suggestion()
            return False
        threading.Thread(target=work, daemon=True).start()
        return False

    def set_branch_suggestion(self):
        if self.branch_auto:
            self.suppress = True
            self.branch_entry.set_text(self.default_branch_name())
            self.suppress = False

    def on_branch_edited(self, *_):
        if not self.suppress:
            self.branch_auto = not self.branch_entry.get_text().strip()

    # ---------- loading
    def on_load(self):
        spec = self.repo_entry.get_text().strip()
        try:
            repo = Repo(spec, self.log)
        except GitError as e:
            self.error(e)
            return False
        self.repo = None
        self.clear_commits("Loading…")
        self.result_bar.set_visible(False)
        self.set_status(f"Fetching {spec}…")
        self.run_bg(repo.sync, lambda res: self.on_loaded(repo, *res),
                    lambda e: (self.set_status(""), self.clear_commits("Could not load the repository."), self.error(e)))
        return False

    def on_loaded(self, repo, branches, default):
        self.repo = repo
        self.set_status(f"{len(branches)} branches")
        prefs = self.cfg.setdefault("repos", {}).get(repo.spec, {})
        target = prefs.get("target") if prefs.get("target") in branches else (default or (branches[0] if branches else None))
        others = [b for b in branches if b != target]
        source = prefs.get("source") if prefs.get("source") in others else (others[0] if others else target)
        self.suppress = True
        for dd in (self.src_dd, self.dst_dd):
            m = dd.get_model()
            m.splice(0, m.get_n_items(), branches)
        self.dd_select(self.src_dd, source)
        self.dd_select(self.dst_dd, target)
        self.suppress = False
        recent = [repo.spec] + [r for r in self.cfg.get("recent", []) if r != repo.spec]
        self.cfg["recent"] = recent[:15]
        save_config(self.cfg)
        self.rebuild_recent()
        self.update_sensitivity()
        self.load_commits()

    def on_refresh(self):
        if not self.repo:
            return self.on_load()
        repo = self.repo
        self.set_status("Fetching…")

        def done(res):
            branches, _ = res
            src, dst = self.dd_value(self.src_dd), self.dd_value(self.dst_dd)
            self.suppress = True
            for dd in (self.src_dd, self.dst_dd):
                m = dd.get_model()
                m.splice(0, m.get_n_items(), branches)
            self.dd_select(self.src_dd, src)
            self.dd_select(self.dst_dd, dst)
            self.suppress = False
            self.set_status(f"{len(branches)} branches")
            self.load_commits()
        self.run_bg(repo.sync, done)

    def on_branch_changed(self, *_):
        if not self.suppress:
            self.load_commits()

    def on_swap(self):
        src, dst = self.dd_value(self.src_dd), self.dd_value(self.dst_dd)
        self.suppress = True
        self.dd_select(self.src_dd, dst)
        self.dd_select(self.dst_dd, src)
        self.suppress = False
        self.load_commits()

    def clear_commits(self, msg):
        self.listbox.remove_all()
        self.rows = []
        self.placeholder.set_text(msg)
        self.diff.get_buffer().set_text("")
        self.update_count()

    def load_commits(self):
        if not self.repo:
            return
        src, dst = self.dd_value(self.src_dd), self.dd_value(self.dst_dd)
        if not src or not dst:
            return
        self.set_branch_suggestion()
        if src == dst:
            self.clear_commits("Pick two different branches.")
            return
        self.cfg.setdefault("repos", {})[self.repo.spec] = {"source": src, "target": dst}
        save_config(self.cfg)
        self.clear_commits("Loading commits…")
        self.suggested_prs = []
        self.set_branch_suggestion()
        repo, merges = self.repo, self.merges_cb.get_active()

        def done(commits):
            if (self.dd_value(self.src_dd), self.dd_value(self.dst_dd)) != (src, dst):
                return  # selection changed meanwhile
            self.clear_commits(f"No commits on {src} that are missing from {dst}.")
            for c in commits:
                row = CommitRow(c, self.on_selection_changed)
                self.rows.append(row)
                self.listbox.append(row)
            self.update_count()
        self.run_bg(lambda: repo.commits(dst, src, merges), done)

    def on_row_selected(self, _lb, row):
        if row is None or not self.repo:
            return
        self.diff_req += 1
        req, repo, sha = self.diff_req, self.repo, row.commit["sha"]

        def work():
            try:
                text = repo.show(sha)
            except GitError as e:
                text = str(e)
            GLib.idle_add(self.show_diff, req, text)
        threading.Thread(target=work, daemon=True).start()

    def show_diff(self, req, text):
        if req != self.diff_req:
            return False
        buf = self.diff.get_buffer()
        buf.set_text("")
        it = buf.get_end_iter()
        in_header = True
        for line in text.splitlines(True):
            tag = None
            if line.startswith("diff --git"):
                in_header, tag = False, "file"
            elif in_header:
                tag = "head" if re.match(r"^(commit|Author|Commit|Merge)\b", line) else None
            elif line.startswith(("+++", "---", "index ", "new file", "deleted file", "similarity", "rename ")):
                tag = "file"
            elif line.startswith("@@"):
                tag = "hunk"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            if tag:
                buf.insert_with_tags_by_name(it, line, tag)
            else:
                buf.insert(it, line)
        buf.place_cursor(buf.get_start_iter())
        self.diff.scroll_to_iter(buf.get_start_iter(), 0, False, 0, 0)
        return False

    # ---------- cherry-picking
    def on_go(self):
        commits = self.selected_commits()
        if not commits or not self.repo:
            return
        commits.reverse()  # list is newest first; apply oldest first
        target = self.dd_value(self.dst_dd)
        branch = self.branch_entry.get_text().strip() or self.default_branch_name()
        self.cfg["record_origin"] = self.x_cb.get_active()
        self.cfg["push"] = self.push_cb.get_active()
        save_config(self.cfg)
        self.queue, self.qi = commits, 0
        self.stats = {"ok": [], "dropped": [], "skipped": []}
        self.last_branch, self.last_target = branch, target
        self.picking = True
        self.result_bar.set_visible(False)
        self.log(f"--- cherry-picking {len(commits)} commit(s) onto new branch {branch} (from origin/{target})")
        self.set_status(f"Creating {branch}…")
        self.run_bg(lambda: self.repo.start_branch(target, branch), lambda _: self.pick_next(),
                    lambda e: (self.end_picking(), self.error(e)))

    def pick_next(self):
        if self.qi >= len(self.queue):
            return self.finish()
        c = self.queue[self.qi]
        self.set_status(f"Picking {self.qi + 1}/{len(self.queue)}: {c['short']} {c['subject']}")
        record = self.x_cb.get_active()
        self.run_bg(lambda: self.repo.pick(c["sha"], record, c["merge"]), self.after_pick,
                    lambda e: (self.finish(stopped=True), self.error(e)))

    def after_pick(self, result):
        c = self.queue[self.qi]
        if result == "conflict":
            self.run_bg(self.repo.conflicts, self.show_conflict)
            return
        self.stats[result].append(c)
        if result == "dropped":
            self.log(f"{c['short']}: changes already present on target - nothing to commit, skipped")
        self.qi += 1
        self.pick_next()

    def show_conflict(self, files):
        self.conflict_files = files
        c = self.queue[self.qi]
        self.set_status(f"Conflict in {c['short']}")
        listing = "\n".join(f"  • {f}" for f in files)
        self.conflict_lbl.set_markup(
            f"<b>Conflict while picking {c['short']} “{GLib.markup_escape_text(c['subject'])}”</b> "
            f"({self.qi + 1} of {len(self.queue)})\n"
            f"Files with conflicts:\n{GLib.markup_escape_text(listing)}\n"
            f"Fix them in <tt>{GLib.markup_escape_text(str(self.repo.path))}</tt> and press Continue, "
            "or resolve all files one way with the buttons below.")
        self.conflict_bar.set_visible(True)
        if self.auto_editor_cb.get_active():
            self.open_conflicts_in_editor()

    def on_resolve(self, side):
        self.run_bg(lambda: (self.repo.resolve_all(side), self.repo.continue_pick())[1], self.after_resolve)

    def on_continue(self):
        self.run_bg(self.repo.continue_pick, self.after_resolve)

    def after_resolve(self, result):
        self.conflict_bar.set_visible(False)
        self.after_pick(result)

    def on_skip(self):
        def done(_):
            self.conflict_bar.set_visible(False)
            self.stats["skipped"].append(self.queue[self.qi])
            self.qi += 1
            self.pick_next()
        self.run_bg(self.repo.skip, done)

    def on_stop(self):
        self.run_bg(self.repo.abort, lambda _: self.finish(stopped=True))

    def end_picking(self):
        self.picking = False
        self.conflict_bar.set_visible(False)
        self.update_sensitivity()

    def finish(self, stopped=False):
        self.end_picking()
        ok, dropped, skipped = self.stats["ok"], self.stats["dropped"], self.stats["skipped"]
        parts = [f"<b>{len(ok)} commit{'s' if len(ok) != 1 else ''} picked</b> onto "
                 f"<tt>{GLib.markup_escape_text(self.last_branch)}</tt> (local)"]
        if dropped:
            parts.append(f"{len(dropped)} already present")
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        if stopped:
            remaining = len(self.queue) - self.qi
            parts.append(f"stopped with {remaining} not picked")
        self.result_lbl.set_markup(", ".join(parts) + ".")
        self.set_status("Stopped" if stopped else "Done")
        self.push_btn.set_visible(bool(ok))
        self.pr_btn.set_visible(False)
        self.result_bar.set_visible(True)
        self.branch_auto = True
        if ok and self.push_cb.get_active():
            self.on_push()
        return False

    def on_push(self):
        branch = self.last_branch
        if not branch or not self.repo:
            return
        self.set_status(f"Pushing {branch}…")

        def done(_):
            self.set_status(f"Pushed {branch}")
            self.result_lbl.set_markup(self.result_lbl.get_label().replace("(local)", "(pushed)"))
            self.push_btn.set_visible(False)
            self.pr_btn.set_visible(self.repo.compare_url(self.last_target, branch) is not None)
        self.run_bg(lambda: self.repo.push(branch), done)

    def on_open_pr(self):
        url = self.repo and self.repo.compare_url(self.last_target, self.last_branch)
        if url:
            Gio.AppInfo.launch_default_for_uri(url, None)


def iter_children(widget):
    child = widget.get_first_child()
    while child:
        yield child
        child = child.get_next_sibling()


class App(Gtk.Application):
    def __init__(self, initial_repo):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.initial_repo = initial_repo

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Window(self, self.initial_repo).present()


if __name__ == "__main__":
    App(sys.argv[1] if len(sys.argv) > 1 else None).run([sys.argv[0]])
