"""The Qt main window. Import this only after cherry_picker.pyside.activate() has run."""
import base64
import html
import shlex
import sys
import threading

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontDatabase, QIcon,
                           QKeySequence, QSyntaxHighlighter, QTextCharFormat)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter,
                               QDockWidget, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMenu, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter,
                               QStackedWidget, QStyle, QTabWidget, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import __version__
from .core import (COMMIT_LIMIT, GitError, Repo, branch_name, editor_args, editor_icon_path, guess_promote_target,
                   installed_editors, launch, load_config, pr_from_subject, promote_branch_name, save_config)

COL_CHECK, COL_SHA, COL_SUBJECT, COL_PR, COL_AUTHOR, COL_DATE = range(6)
P_SHA, P_SUBJECT, P_PR, P_AUTHOR, P_DATE = range(5)  # columns of the Promote tab's list


def icon(*names):
    """First icon from the theme that exists (Breeze names first, generic fallbacks after)."""
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QIcon()


class Bridge(QObject):
    """Runs callables on the GUI thread when emitted from worker threads."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    @Slot(object)
    def _run(self, fn):
        fn()


class DiffHighlighter(QSyntaxHighlighter):
    HEADER, BODY = 0, 1

    def __init__(self, doc):
        super().__init__(doc)

        def fmt(color=None, bold=False):
            f = QTextCharFormat()
            if color:
                f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            return f
        # Breeze palette: positive / negative / link
        self.f_add, self.f_del, self.f_hunk = fmt("#27ae60"), fmt("#da4453"), fmt("#3daee9")
        self.f_bold = fmt(bold=True)

    def highlightBlock(self, text):
        state = self.previousBlockState()
        state = self.HEADER if state < 0 else state
        f = None
        if text.startswith("diff --git"):
            state, f = self.BODY, self.f_bold
        elif state == self.HEADER:
            if text.startswith(("commit ", "Author:", "Commit:", "Merge:")):
                f = self.f_bold
        elif text.startswith(("+++", "---", "index ", "new file", "deleted file", "similarity", "rename ")):
            f = self.f_bold
        elif text.startswith("@@"):
            f = self.f_hunk
        elif text.startswith("+"):
            f = self.f_add
        elif text.startswith("-"):
            f = self.f_del
        if f:
            self.setFormat(0, len(text), f)
        self.setCurrentBlockState(state)


class MessageWidget(QFrame):
    """Inline message in the style of KDE's KMessageWidget."""
    COLORS = {"information": "#3daee9", "positive": "#27ae60", "warning": "#f67400", "error": "#da4453"}
    ICONS = {"information": "dialog-information", "positive": "dialog-positive",
             "warning": "dialog-warning", "error": "dialog-error"}

    def __init__(self, kind="information", closable=False):
        super().__init__()
        self.setObjectName("message")
        outer = QHBoxLayout(self)
        self.icon_lbl = QLabel()
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(self.icon_lbl)
        body = QVBoxLayout()
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.addWidget(self.text)
        self.buttons = QHBoxLayout()
        body.addLayout(self.buttons)
        outer.addLayout(body, 1)
        if closable:
            close = QToolButton()
            close.setIcon(icon("dialog-close", "window-close"))
            close.setAutoRaise(True)
            close.setToolTip("Close")
            close.clicked.connect(self.hide)
            outer.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.set_kind(kind)
        self.hide()

    def set_kind(self, kind):
        c = QColor(self.COLORS[kind])
        r, g, b = c.red(), c.green(), c.blue()
        self.setStyleSheet(f"QFrame#message {{ background-color: rgba({r},{g},{b},0.2);"
                           f" border: 1px solid rgb({r},{g},{b}); border-radius: 5px; }}")
        self.icon_lbl.setPixmap(icon(self.ICONS[kind]).pixmap(QSize(22, 22)))

    def show_message(self, kind, text):
        self.set_kind(kind)
        self.text.setText(text)
        self.show()

    def add_button(self, button):
        self.buttons.addWidget(button)
        return button

    def add_stretch(self):
        self.buttons.addStretch(1)


class Placeholder(QWidget):
    """Empty-state message in the style of Kirigami's PlaceholderMessage."""

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        self.icon_lbl = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl.setEnabled(False)  # disabled = dimmed, like Kirigami
        lay.addWidget(self.icon_lbl)
        self.title = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        self.title.setFont(f)
        self.title.setEnabled(False)
        lay.addWidget(self.title)
        self.explanation = QLabel(alignment=Qt.AlignmentFlag.AlignCenter, wordWrap=True)
        self.explanation.setEnabled(False)
        lay.addWidget(self.explanation)
        lay.addStretch(2)

    def set(self, icon_names, title, explanation=""):
        self.icon_lbl.setPixmap(icon(*icon_names).pixmap(QSize(64, 64)))
        self.title.setText(title)
        self.explanation.setText(explanation)
        self.explanation.setVisible(bool(explanation))


def push_button(text, cb, tooltip=None, icons=()):
    b = QPushButton(icon(*icons), text) if icons else QPushButton(text)
    if tooltip:
        b.setToolTip(tooltip)
    b.clicked.connect(cb)
    return b


def searchable_combo(min_width=240):
    """A branch picker you can type into to filter (contains-match)."""
    cb = QComboBox()
    cb.setEditable(True)
    cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    cb.setMinimumWidth(min_width)
    cb.completer().setFilterMode(Qt.MatchFlag.MatchContains)
    cb.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    return cb


class Window(QMainWindow):
    def __init__(self, initial_repo=None):
        super().__init__()
        self.setWindowTitle(QApplication.applicationDisplayName())  # "Cherry Picker <version>"
        self.cfg = load_config()
        self.bridge = Bridge()
        self.repo = None
        self.busy = 0
        self.picking = False
        self.branch_auto = True
        self.diff_req = self.pr_req = 0
        self.suggested_prs = []
        self.conflict_files = []
        self.queue, self.qi, self.stats = [], 0, {}
        self.last_branch = self.last_target = None
        self.result_template = ""
        self.p_branch_auto = True
        self.p_total = 0  # commits the promote merge would bring in
        self.p_diff_req = 0
        self.p_last = {}  # source / target / branch / fork of the last promotion
        self.p_result_template = ""
        self.editors = installed_editors()
        spacing = self.style().pixelMetric(QStyle.PixelMetric.PM_LayoutHorizontalSpacing) or 6

        # ------------------------------------------------ toolbar
        tb = QToolBar("Main Toolbar")
        tb.setObjectName("mainToolBar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(tb)
        repo_lbl = QLabel("Repository:")
        repo_lbl.setContentsMargins(spacing, 0, spacing, 0)
        tb.addWidget(repo_lbl)
        self.repo_combo = QComboBox()
        self.repo_combo.setEditable(True)
        self.repo_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.repo_combo.setMinimumWidth(380)
        self.repo_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.repo_combo.lineEdit().setPlaceholderText("owner/repo, GitHub URL or git URL")
        self.repo_combo.setToolTip("GitHub repository to cherry-pick in")
        self.repo_combo.lineEdit().returnPressed.connect(self.on_load)
        self.repo_combo.addItems(self.cfg.get("recent", []))
        self.repo_combo.setCurrentIndex(-1)
        self.repo_combo.activated.connect(lambda _i: self.on_load())
        repo_lbl.setBuddy(self.repo_combo)
        tb.addWidget(self.repo_combo)
        self.load_act = tb.addAction(icon("vcs-pull", "download", "go-down"), "Load", self.on_load)
        self.load_act.setToolTip("Clone or update the repository")
        self.fetch_act = tb.addAction(icon("view-refresh"), "Fetch", self.on_refresh)
        self.fetch_act.setShortcut(QKeySequence.StandardKey.Refresh)
        self.fetch_act.setToolTip("Fetch the latest branches and commits (F5)")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        self.menu_btn = QToolButton()
        self.menu_btn.setIcon(icon("application-menu", "open-menu-symbolic", "preferences-other"))
        self.menu_btn.setToolTip("Main Menu")
        self.menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(self.menu_btn)

        # ------------------------------------------------ central area
        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        self.error_msg = MessageWidget("error", closable=True)
        root.addWidget(self.error_msg)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        root.addWidget(self.tabs, 1)
        pick_tab = QWidget()
        i = self.tabs.addTab(pick_tab, icon("vcs-commit", "edit-copy"), "Cherry-Pick")
        self.tabs.setTabToolTip(i, "Copy chosen commits from one branch onto another")
        root = QVBoxLayout(pick_tab)

        row = QHBoxLayout()
        src_lbl = QLabel("Pick from:")
        row.addWidget(src_lbl)
        self.src_combo = searchable_combo()
        self.src_combo.setToolTip("Branch that has the commits you want")
        src_lbl.setBuddy(self.src_combo)
        row.addWidget(self.src_combo)
        self.swap_btn = QToolButton()
        self.swap_btn.setIcon(icon("exchange-positions", "object-flip-horizontal", "view-refresh"))
        self.swap_btn.setToolTip("Swap branches")
        self.swap_btn.clicked.connect(self.on_swap)
        row.addWidget(self.swap_btn)
        dst_lbl = QLabel("Onto:")
        row.addWidget(dst_lbl)
        self.dst_combo = searchable_combo()
        self.dst_combo.setToolTip("Branch the commits should land on")
        dst_lbl.setBuddy(self.dst_combo)
        row.addWidget(self.dst_combo)
        row.addStretch(1)
        for cb in (self.src_combo, self.dst_combo):
            cb.activated.connect(lambda _i: self.load_commits())
            cb.lineEdit().returnPressed.connect(self.load_commits)
        root.addLayout(row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.split = split
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter…")
        self.search.setToolTip("Filter by message, author, hash or #PR (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon("search", "edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda _t: self.apply_filter())
        tools.addWidget(self.search, 1)
        tools.addWidget(push_button("Select All", lambda: self.select_visible(True), "Select every visible commit",
                                    ("edit-select-all",)))
        tools.addWidget(push_button("Deselect All", lambda: self.select_visible(False), "Clear the selection",
                                    ("edit-select-none",)))
        view_btn = QToolButton()
        view_btn.setIcon(icon("view-filter", "view-more-symbolic"))
        view_btn.setToolTip("View Options")
        view_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        view_menu = QMenu(view_btn)
        self.merges_act = view_menu.addAction("Show Merge Commits")
        self.merges_act.setCheckable(True)
        self.merges_act.toggled.connect(lambda _on: self.load_commits())
        self.applied_act = view_menu.addAction("Show Commits Already in Target")
        self.applied_act.setCheckable(True)
        self.applied_act.toggled.connect(lambda _on: self.apply_filter())
        view_btn.setMenu(view_menu)
        tools.addWidget(view_btn)
        lv.addLayout(tools)

        self.list_stack = QStackedWidget()
        self.placeholder = Placeholder()
        self.list_stack.addWidget(self.placeholder)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "Hash", "Subject", "PR", "Author", "Date"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        for col, mode in ((COL_CHECK, QHeaderView.ResizeMode.ResizeToContents),
                          (COL_SHA, QHeaderView.ResizeMode.ResizeToContents),
                          (COL_SUBJECT, QHeaderView.ResizeMode.Stretch),
                          (COL_PR, QHeaderView.ResizeMode.ResizeToContents),
                          (COL_AUTHOR, QHeaderView.ResizeMode.Interactive),
                          (COL_DATE, QHeaderView.ResizeMode.ResizeToContents)):
            hdr.setSectionResizeMode(col, mode)
        self.tree.setColumnWidth(COL_AUTHOR, 140)
        self.tree.itemChanged.connect(self.on_item_changed)
        self.tree.currentItemChanged.connect(self.on_current_changed)
        self.tree.itemActivated.connect(self.toggle_item)
        self.list_stack.addWidget(self.tree)
        lv.addWidget(self.list_stack, 1)
        self.count_lbl = QLabel()
        self.count_lbl.setEnabled(False)
        lv.addWidget(self.count_lbl)
        split.addWidget(left)

        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.diff.setPlaceholderText("Select a commit to see its changes")
        self.highlighter = DiffHighlighter(self.diff.document())
        split.addWidget(self.diff)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        # conflict message
        self.conflict_msg = MessageWidget("warning")
        m = self.conflict_msg
        self.editor_btn = m.add_button(push_button("Open in Editor", self.open_conflicts_in_editor,
                                                   "Open the conflicted files", ("document-edit",)))
        m.add_button(push_button("Open Folder", self.open_folder, "Open the working copy in the file manager",
                                 ("document-open-folder", "folder-open")))
        m.add_button(push_button("Take Incoming", lambda: self.on_resolve("theirs"),
                                 "Resolve every conflicted file with the version from the picked commit",
                                 ("go-next",)))
        m.add_button(push_button("Keep Target", lambda: self.on_resolve("ours"),
                                 "Resolve every conflicted file with the target branch version", ("go-previous",)))
        m.add_stretch()
        m.add_button(push_button("Skip Commit", self.on_skip, "Leave this commit out and go on",
                                 ("media-skip-forward",)))
        m.add_button(push_button("Stop", self.on_stop, "Abort this commit and stop; earlier picks stay on the branch",
                                 ("process-stop",)))
        self.continue_btn = m.add_button(push_button("Continue", self.on_continue,
                                                     "Commit the resolved files and go on", ("dialog-ok-apply",)))
        self.conflict_btns = [m.buttons.itemAt(i).widget() for i in range(m.buttons.count())
                              if isinstance(m.buttons.itemAt(i).widget(), QPushButton)]
        root.addWidget(self.conflict_msg)

        # result message
        self.result_msg = MessageWidget("positive", closable=True)
        r = self.result_msg
        r.add_button(push_button("Open Folder", self.open_folder, icons=("document-open-folder", "folder-open")))
        self.push_btn = r.add_button(push_button("Push", self.on_push, "Push the branch to GitHub",
                                                 ("cloud-upload", "go-up")))
        self.pr_btn = r.add_button(push_button("Create Pull Request…", self.on_open_pr,
                                               "Open GitHub to create the pull request",
                                               ("vcs-merge-request", "internet-web-browser")))
        r.add_stretch()
        root.addWidget(self.result_msg)

        # bottom: branch + options + primary action (bottom right)
        row = QHBoxLayout()
        br_lbl = QLabel("New branch:")
        row.addWidget(br_lbl)
        self.branch_edit = QLineEdit()
        self.branch_edit.setMinimumWidth(300)
        self.branch_edit.setToolTip("Branch created from the target to hold the picks. "
                                    "Use the target branch's own name to commit straight onto it.")
        self.branch_edit.textEdited.connect(self.on_branch_edited)
        br_lbl.setBuddy(self.branch_edit)
        row.addWidget(self.branch_edit)
        row.addSpacing(spacing * 2)
        self.x_cb = QCheckBox("Record origin (-x)")
        self.x_cb.setToolTip("Append “(cherry picked from commit …)” to each commit message")
        self.x_cb.setChecked(self.cfg.get("record_origin", True))
        row.addWidget(self.x_cb)
        self.push_cb = QCheckBox("Push when done")
        self.push_cb.setChecked(self.cfg.get("push", False))
        row.addWidget(self.push_cb)
        row.addStretch(1)
        self.go_btn = push_button("Cherry-Pick", self.on_go, "Apply the selected commits (Ctrl+Return)",
                                  ("vcs-commit", "dialog-ok-apply"))
        self.go_btn.setDefault(True)
        row.addWidget(self.go_btn)
        root.addLayout(row)

        self.build_promote_tab(spacing)

        # ------------------------------------------------ git output dock
        self.log_dock = QDockWidget("Git Output", self)
        self.log_dock.setObjectName("gitOutputDock")
        self.logview = QPlainTextEdit()
        self.logview.setReadOnly(True)
        self.logview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.logview.setPlaceholderText("Git commands and their output appear here")
        self.log_dock.setWidget(self.logview)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()

        # ------------------------------------------------ status bar
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)
        self.busy_bar.setMaximumWidth(120)
        self.busy_bar.hide()
        self.statusBar().addPermanentWidget(self.busy_bar)

        self.build_menu()
        for seq, fn in (("Ctrl+Return", self.on_primary), (QKeySequence.StandardKey.Find, self.search.setFocus)):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)

        self.pr_timer = QTimer(self, singleShot=True, interval=250)
        self.pr_timer.timeout.connect(self.lookup_prs)

        self.controls = [self.repo_combo, self.load_act, self.fetch_act, self.src_combo, self.dst_combo,
                         self.swap_btn, self.merges_act, self.branch_edit,
                         self.p_src_combo, self.p_dst_combo, self.p_branch_edit, self.p_fork_edit]
        self.restore_layout()
        self.set_placeholder()
        self.update_editor_button()
        self.update_count()

        recent = self.cfg.get("recent", [])
        if initial_repo:
            self.repo_combo.setEditText(initial_repo)
            QTimer.singleShot(0, self.on_load)
        elif recent:
            self.repo_combo.setEditText(recent[0])

    # ------------------------------------------------------------ menu & layout
    def build_menu(self):
        menu = QMenu(self)
        out = self.log_dock.toggleViewAction()
        out.setText("Show Git Output")
        out.setIcon(icon("utilities-terminal", "view-list-text"))
        menu.addAction(out)
        self.folder_act = menu.addAction(icon("document-open-folder", "folder-open"), "Open Working Folder",
                                         self.open_folder)
        menu.addSeparator()
        editors = menu.addMenu(icon("document-edit"), "Conflict Editor")
        group = QActionGroup(self)
        saved = self.cfg.get("editor")
        self.editor_index = 0
        for i, (name, _cmd) in enumerate(self.editors):
            a = editors.addAction(name)
            a.setCheckable(True)
            group.addAction(a)
            if name == saved or (i == 0 and saved not in [n for n, _ in self.editors]):
                a.setChecked(True)
                self.editor_index = i
            a.triggered.connect(lambda _c=False, i=i: self.choose_editor(i))
        self.auto_editor_act = menu.addAction("Open Editor Automatically on Conflict")
        self.auto_editor_act.setCheckable(True)
        self.auto_editor_act.setChecked(self.cfg.get("auto_editor", False))
        self.auto_editor_act.toggled.connect(lambda _on: self.save_editor_prefs())
        menu.addSeparator()
        quit_act = menu.addAction(icon("application-exit"), "Quit", self.close)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        self.addAction(quit_act)
        self.menu_btn.setMenu(menu)

    def restore_layout(self):
        ui = self.cfg.get("ui", {})
        try:
            if ui.get("geometry"):
                self.restoreGeometry(base64.b64decode(ui["geometry"]))
            else:
                self.resize(1300, 850)
            if ui.get("state"):
                self.restoreState(base64.b64decode(ui["state"]))
            if ui.get("split"):
                self.split.restoreState(base64.b64decode(ui["split"]))
            else:
                self.split.setSizes([650, 650])
            if ui.get("promote_split"):
                self.p_split.restoreState(base64.b64decode(ui["promote_split"]))
            else:
                self.p_split.setSizes([650, 650])
            self.tabs.setCurrentIndex(int(ui.get("tab", 0)))
        except (ValueError, TypeError):
            self.resize(1300, 850)

    def closeEvent(self, event):
        enc = lambda b: base64.b64encode(bytes(b)).decode()  # noqa: E731
        self.cfg["ui"] = {"geometry": enc(self.saveGeometry()), "state": enc(self.saveState()),
                          "split": enc(self.split.saveState()), "promote_split": enc(self.p_split.saveState()),
                          "tab": self.tabs.currentIndex()}
        save_config(self.cfg)
        super().closeEvent(event)

    # ------------------------------------------------------------ helpers
    def items(self):
        return [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]

    def log(self, text):  # safe from any thread
        self.bridge.call.emit(lambda: self.logview.appendPlainText(text))

    def set_status(self, text):
        self.statusBar().showMessage(text)

    def error(self, exc):
        self.log(f"ERROR: {exc}")
        text = html.escape(str(exc)).replace("\n", "<br>")
        self.error_msg.show_message("error", text)

    def run_bg(self, fn, done=None, fail=None):
        self.busy += 1
        self.update_sensitivity()

        def finish(res, exc):
            self.busy -= 1
            self.update_sensitivity()
            if exc is not None:
                (fail or self.error)(exc)
            elif done:
                done(res)

        def work():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                self.bridge.call.emit(lambda err=e: finish(None, err))  # `e` is cleared after the block
            else:
                self.bridge.call.emit(lambda: finish(res, None))
        threading.Thread(target=work, daemon=True).start()

    def update_sensitivity(self):
        idle = self.busy == 0 and not self.picking
        for w in self.controls:
            w.setEnabled(idle)
        self.go_btn.setEnabled(idle and self.repo is not None and self.selected_count() > 0)
        for b in self.conflict_btns:
            b.setEnabled(self.busy == 0)
        self.push_btn.setEnabled(idle)
        self.p_go_btn.setEnabled(idle and self.repo is not None and self.p_total > 0
                                 and self.p_source() != self.p_target())
        for b in self.p_conflict_btns:
            b.setEnabled(self.busy == 0)
        self.p_push_btn.setEnabled(idle)
        self.folder_act.setEnabled(self.repo is not None)
        self.busy_bar.setVisible(self.busy > 0)

    def set_placeholder(self, icons=("vcs-branch", "folder-git", "git"), title="No repository loaded",
                        explanation="Enter a GitHub repository above and press Load."):
        self.placeholder.set(icons, title, explanation)
        self.list_stack.setCurrentWidget(self.placeholder)

    def selected_commits(self):
        return [it.commit for it in self.items() if it.checkState(COL_CHECK) == Qt.CheckState.Checked]

    def selected_count(self):
        return len(self.selected_commits())

    def update_count(self):
        items = self.items()
        n = self.selected_count()
        hidden = sum(it.isHidden() for it in items)
        txt = f"{len(items)} commits" if items else ""
        if hidden:
            txt += f", {hidden} hidden"
        if len(items) >= COMMIT_LIMIT:
            txt += f" (newest {COMMIT_LIMIT} shown)"
        if n:
            txt += f" · {n} selected"
        self.count_lbl.setText(txt)
        self.go_btn.setText(f"Cherry-Pick {n} Commit{'s' if n != 1 else ''}" if n else "Cherry-Pick")
        self.update_sensitivity()

    def matches(self, c):
        if not self.applied_act.isChecked() and c["applied"]:
            return False
        q = self.search.text().strip().lower()
        if not q:
            return True
        pr = c.get("pr")
        return (q in c["subject"].lower() or q in c["author"].lower() or c["sha"].startswith(q)
                or (pr is not None and q.lstrip("#") == str(pr)))

    def apply_filter(self):
        for it in self.items():
            it.setHidden(not self.matches(it.commit))
        self.update_count()

    def select_visible(self, on):
        self.tree.blockSignals(True)
        for it in self.items():
            if not on:
                it.setCheckState(COL_CHECK, Qt.CheckState.Unchecked)
            elif not it.isHidden():
                it.setCheckState(COL_CHECK, Qt.CheckState.Checked)
        self.tree.blockSignals(False)
        self.tree.viewport().update()
        self.on_selection_changed()

    def toggle_item(self, item, _col=0):
        checked = item.checkState(COL_CHECK) == Qt.CheckState.Checked
        item.setCheckState(COL_CHECK, Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked)

    def on_item_changed(self, _item, col):
        if col == COL_CHECK:
            self.on_selection_changed()

    def remember_recent(self, spec):
        recent = [spec] + [r for r in self.cfg.get("recent", []) if r != spec]
        self.cfg["recent"] = recent[:15]
        save_config(self.cfg)
        self.repo_combo.blockSignals(True)
        self.repo_combo.clear()
        self.repo_combo.addItems(self.cfg["recent"])
        self.repo_combo.setEditText(spec)
        self.repo_combo.blockSignals(False)

    def open_folder(self):
        if self.repo:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.repo.path)))

    # ------------------------------------------------------------ branch name
    def target(self):
        return self.dst_combo.currentText().strip()

    def source(self):
        return self.src_combo.currentText().strip()

    def default_branch_name(self):
        return branch_name(self.target() or "target", self.suggested_prs)

    def set_branch_suggestion(self):
        if self.branch_auto:
            self.branch_edit.setText(self.default_branch_name())

    def on_branch_edited(self, text):
        self.branch_auto = not text.strip()

    def on_selection_changed(self):
        self.update_count()
        self.pr_timer.start()  # debounce: "Select All" toggles many rows at once

    def lookup_prs(self):
        """Find PR numbers of the selected commits (oldest first) for the branch name."""
        self.pr_req += 1
        req, repo = self.pr_req, self.repo
        commits = list(reversed(self.selected_commits()))
        if not repo or not commits:
            self.suggested_prs = []
            self.set_branch_suggestion()
            return

        def done(found):
            for it in self.items():  # also show numbers found via the GitHub API
                n = found.get(it.commit["sha"])
                if n and not it.text(COL_PR):
                    it.commit["pr"] = n
                    it.setText(COL_PR, f"#{n}")
            if req == self.pr_req:
                prs = []
                for c in commits:
                    n = found.get(c["sha"])
                    if n and n not in prs:
                        prs.append(n)
                self.suggested_prs = prs
                self.set_branch_suggestion()

        def work():
            found = {c["sha"]: repo.pr_number(c) for c in commits}
            self.bridge.call.emit(lambda: done(found))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ loading
    def on_load(self):
        spec = self.repo_combo.currentText().strip()
        if self.busy or self.picking:
            return
        try:
            repo = Repo(spec, self.log)
        except GitError as e:
            self.error(e)
            return
        self.repo = None
        self.clear_commits()
        self.clear_promote()
        self.p_result_msg.hide()
        self.set_p_placeholder(("download", "folder-download"), "Loading repository…")
        self.error_msg.hide()
        self.result_msg.hide()
        self.set_placeholder(("download", "folder-download"), "Loading repository…",
                             "The first load clones the repository, which can take a while.")
        self.set_status(f"Fetching {spec}…")
        self.run_bg(repo.sync, lambda res: self.on_loaded(repo, *res), self.on_load_failed)

    def on_load_failed(self, e):
        self.set_status("Could not load the repository")
        self.set_placeholder(("dialog-error",), "Could not load the repository",
                             "Check the name and your access, then press Load again.")
        self.set_p_placeholder(("dialog-error",), "Could not load the repository")
        self.error(e)

    def fill_branches(self, branches, source, target, combos=None):
        src_cb, dst_cb = combos or (self.src_combo, self.dst_combo)
        for cb, value in ((src_cb, source), (dst_cb, target)):
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(branches)
            i = cb.findText(value) if value else -1
            cb.setCurrentIndex(i if i >= 0 else 0)
            cb.blockSignals(False)

    def on_loaded(self, repo, branches, default):
        self.repo = repo
        self.setWindowTitle(f"{repo.slug or repo.spec} — {QApplication.applicationDisplayName()}")  # Qt won't append the name twice
        self.set_status(f"{len(branches)} branches")
        prefs = self.cfg.setdefault("repos", {}).get(repo.spec, {})
        target = prefs.get("target") if prefs.get("target") in branches else (default or (branches[0] if branches else None))
        others = [b for b in branches if b != target]
        source = prefs.get("source") if prefs.get("source") in others else (others[0] if others else target)
        self.fill_branches(branches, source, target)
        pp = prefs.get("promote", {})
        p_src = pp.get("source") if pp.get("source") in branches else (default or (branches[0] if branches else None))
        p_dst = pp.get("target") if pp.get("target") in branches and pp.get("target") != p_src \
            else guess_promote_target(branches, p_src)
        self.fill_branches(branches, p_src, p_dst, (self.p_src_combo, self.p_dst_combo))
        self.p_fork_edit.setText(pp.get("fork", ""))
        self.p_branch_auto = True
        self.remember_recent(repo.spec)
        self.update_sensitivity()
        self.load_commits()
        self.load_promote()

    def on_refresh(self):
        if not self.repo:
            return self.on_load()
        if self.busy or self.picking:
            return
        repo, src, dst = self.repo, self.source(), self.target()
        p_src, p_dst = self.p_source(), self.p_target()
        self.set_status("Fetching…")

        def done(res):
            branches, _ = res
            self.fill_branches(branches, src, dst)
            self.fill_branches(branches, p_src, p_dst, (self.p_src_combo, self.p_dst_combo))
            self.set_status(f"{len(branches)} branches")
            self.load_commits()
            self.load_promote()
        self.run_bg(repo.sync, done)

    def on_swap(self):
        src, dst = self.source(), self.target()
        for cb, value in ((self.src_combo, dst), (self.dst_combo, src)):
            cb.blockSignals(True)
            cb.setCurrentIndex(max(cb.findText(value), 0))
            cb.blockSignals(False)
        self.load_commits()

    def clear_commits(self):
        self.tree.clear()
        self.diff.clear()
        self.suggested_prs = []
        self.update_count()

    def load_commits(self):
        if not self.repo:
            return
        src, dst = self.source(), self.target()
        branches = [self.src_combo.itemText(i) for i in range(self.src_combo.count())]
        if src not in branches or dst not in branches:
            return
        self.clear_commits()
        self.set_branch_suggestion()
        if src == dst:
            self.set_placeholder(("vcs-branch", "dialog-information"), "Pick two different branches")
            return
        self.cfg.setdefault("repos", {}).setdefault(self.repo.spec, {}).update(source=src, target=dst)
        save_config(self.cfg)
        self.set_status(f"Loading commits on {src} missing from {dst}…")
        self.set_placeholder(("view-history", "document-open-recent"), "Loading commits…")
        repo, merges = self.repo, self.merges_act.isChecked()

        def done(commits):
            if (self.source(), self.target()) != (src, dst):
                return  # selection changed meanwhile
            self.tree.blockSignals(True)
            dim = self.palette().placeholderText().color()
            mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            for c in commits:
                c["pr"] = pr_from_subject(c["subject"])
                it = QTreeWidgetItem(["", c["short"], c["subject"], f"#{c['pr']}" if c["pr"] else "",
                                      c["author"], c["date"]])
                it.commit = c
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(COL_CHECK, Qt.CheckState.Unchecked)
                it.setFont(COL_SHA, mono)
                tip = c["subject"]
                if c["merge"]:
                    tip += "\nMerge commit: picked against its first parent"
                if c["applied"]:
                    tip += "\nAlready in the target branch"
                    for col in range(6):
                        it.setForeground(col, dim)
                    f = it.font(COL_SUBJECT)
                    f.setItalic(True)
                    it.setFont(COL_SUBJECT, f)
                it.setToolTip(COL_SUBJECT, tip)
                self.tree.addTopLevelItem(it)
            self.tree.blockSignals(False)
            self.apply_filter()
            if commits:
                self.list_stack.setCurrentWidget(self.tree)
                self.set_status(f"{len(commits)} commits on {src} missing from {dst}")
            else:
                self.set_placeholder(("checkmark", "dialog-ok"), "Nothing to cherry-pick",
                                     f"Every commit on {src} is already in {dst}.")
                self.set_status("")
        self.run_bg(lambda: repo.commits(dst, src, merges), done,
                    lambda e: (self.set_placeholder(("dialog-error",), "Could not list commits"), self.error(e)))

    def on_current_changed(self, item, _prev):
        if item is None or not self.repo:
            return
        self.diff_req += 1
        req, repo, sha = self.diff_req, self.repo, item.commit["sha"]

        def work():
            try:
                text = repo.show(sha)
            except GitError as e:
                text = str(e)
            self.bridge.call.emit(lambda: req == self.diff_req and self.diff.setPlainText(text))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ editor
    def choose_editor(self, i):
        self.editor_index = i
        self.save_editor_prefs()
        self.update_editor_button()

    def update_editor_button(self):
        name, cmd = self.editors[self.editor_index]
        ic = None
        if cmd:
            path = editor_icon_path(cmd)
            ic = QIcon(path) if path else icon(cmd, {"zed": "dev.zed.Zed", "code": "visual-studio-code",
                                                     "kate": "org.kde.kate", "kwrite": "org.kde.kwrite"}
                                               .get(cmd, cmd), "document-edit")
        for b in (self.editor_btn, self.p_editor_btn):
            b.setText("Open in Editor" if cmd is None else f"Open in {name}")
            if ic:
                b.setIcon(ic)

    def save_editor_prefs(self):
        self.cfg["editor"] = self.editors[self.editor_index][0]
        self.cfg["auto_editor"] = self.auto_editor_act.isChecked()
        save_config(self.cfg)

    def open_conflicts_in_editor(self):
        if not self.repo:
            return
        files = [str(self.repo.path / f) for f in self.conflict_files if (self.repo.path / f).is_file()]
        name, cmd = self.editors[self.editor_index]
        try:
            if cmd is None:
                for f in files:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(f))
            else:
                args = editor_args(cmd, self.repo.path, files)
                self.log("$ " + shlex.join(args))
                launch(args, self.repo.path)
        except OSError as e:
            self.error(GitError(f"Could not start {name}: {e}"))

    # ------------------------------------------------------------ cherry-picking
    def on_primary(self):  # Ctrl+Return
        (self.on_promote if self.tabs.currentWidget() is self.promote_tab else self.on_go)()

    def on_go(self):
        commits = self.selected_commits()
        if not commits or not self.repo or self.busy or self.picking:
            return
        commits.reverse()  # list is newest first; apply oldest first
        target = self.target()
        branch = self.branch_edit.text().strip() or self.default_branch_name()
        self.cfg["record_origin"] = self.x_cb.isChecked()
        self.cfg["push"] = self.push_cb.isChecked()
        save_config(self.cfg)
        self.queue, self.qi = commits, 0
        self.stats = {"ok": [], "dropped": [], "skipped": []}
        self.last_branch, self.last_target = branch, target
        self.picking = True
        self.result_msg.hide()
        self.error_msg.hide()
        self.log(f"--- cherry-picking {len(commits)} commit(s) onto new branch {branch} (from origin/{target})")
        self.set_status(f"Creating {branch}…")
        self.run_bg(lambda: self.repo.new_branch(target, branch), lambda _: self.pick_next(),
                    lambda e: (self.end_picking(), self.error(e)))

    def pick_next(self):
        if self.qi >= len(self.queue):
            return self.finish()
        c = self.queue[self.qi]
        self.set_status(f"Picking {self.qi + 1} of {len(self.queue)}: {c['short']} {c['subject']}")
        record = self.x_cb.isChecked()
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
        esc = html.escape
        listing = "<br>".join(f"&nbsp;&nbsp;•&nbsp;<tt>{esc(f)}</tt>" for f in files)
        self.conflict_msg.show_message(
            "warning",
            f"<b>Conflict while picking {c['short']} “{esc(c['subject'])}”</b> "
            f"({self.qi + 1} of {len(self.queue)})<br>{listing}<br>"
            "Resolve the conflicts and press <b>Continue</b>, or settle every file one way with "
            "<i>Take Incoming</i> or <i>Keep Target</i>.")
        self.continue_btn.setDefault(True)
        self.continue_btn.setFocus()
        if self.auto_editor_act.isChecked():
            self.open_conflicts_in_editor()

    def on_resolve(self, side):
        self.run_bg(lambda: (self.repo.resolve_all(side), self.repo.continue_pick())[1], self.after_resolve)

    def on_continue(self):
        self.run_bg(self.repo.continue_pick, self.after_resolve)

    def after_resolve(self, result):
        self.conflict_msg.hide()
        self.after_pick(result)

    def on_skip(self):
        def done(_):
            self.conflict_msg.hide()
            self.stats["skipped"].append(self.queue[self.qi])
            self.qi += 1
            self.pick_next()
        self.run_bg(self.repo.skip, done)

    def on_stop(self):
        self.run_bg(self.repo.abort, lambda _: self.finish(stopped=True))

    def end_picking(self):
        self.picking = False
        self.conflict_msg.hide()
        self.update_sensitivity()

    def finish(self, stopped=False):
        self.end_picking()
        ok, dropped, skipped = self.stats["ok"], self.stats["dropped"], self.stats["skipped"]
        parts = [f"<b>{len(ok)} commit{'s' if len(ok) != 1 else ''} picked</b> onto "
                 f"<tt>{html.escape(self.last_branch)}</tt> (%STATE%)"]
        if dropped:
            parts.append(f"{len(dropped)} already present")
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        if stopped:
            parts.append(f"stopped with {len(self.queue) - self.qi} not picked")
        self.result_template = ", ".join(parts) + "."
        kind = "positive" if ok and not stopped else "information"
        self.result_msg.show_message(kind, self.result_template.replace("%STATE%", "local"))
        self.set_status("Stopped" if stopped else "Done")
        self.push_btn.setVisible(bool(ok))
        self.pr_btn.hide()
        self.branch_auto = True
        if ok and self.push_cb.isChecked():
            self.on_push()

    def on_push(self):
        branch = self.last_branch
        if not branch or not self.repo:
            return
        self.set_status(f"Pushing {branch}…")

        def done(_):
            self.set_status(f"Pushed {branch}")
            self.result_msg.text.setText(self.result_template.replace("%STATE%", "pushed"))
            self.push_btn.hide()
            self.pr_btn.setVisible(self.repo.compare_url(self.last_target, branch) is not None)
        self.run_bg(lambda: self.repo.push(branch), done)

    def on_open_pr(self):
        url = self.repo and self.repo.compare_url(self.last_target, self.last_branch)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------ promote tab
    def build_promote_tab(self, spacing):
        tab = self.promote_tab = QWidget()
        i = self.tabs.addTab(tab, icon("vcs-merge", "merge", "go-up"), "Promote")
        self.tabs.setTabToolTip(i, "Merge a whole branch into another, e.g. main into a stable branch")
        root = QVBoxLayout(tab)

        row = QHBoxLayout()
        src_lbl = QLabel("Promote:")
        row.addWidget(src_lbl)
        self.p_src_combo = searchable_combo()
        self.p_src_combo.setToolTip("Branch whose commits are merged in (usually main)")
        src_lbl.setBuddy(self.p_src_combo)
        row.addWidget(self.p_src_combo)
        dst_lbl = QLabel("Into:")
        row.addWidget(dst_lbl)
        self.p_dst_combo = searchable_combo()
        self.p_dst_combo.setToolTip("Branch that receives them, e.g. a stable branch")
        dst_lbl.setBuddy(self.p_dst_combo)
        row.addWidget(self.p_dst_combo)
        row.addStretch(1)
        for cb in (self.p_src_combo, self.p_dst_combo):
            cb.activated.connect(lambda _i: self.on_promote_branches_changed())
            cb.lineEdit().returnPressed.connect(self.on_promote_branches_changed)
        root.addLayout(row)

        split = self.p_split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.p_stack = QStackedWidget()
        self.p_placeholder = Placeholder()
        self.p_stack.addWidget(self.p_placeholder)
        self.p_tree = QTreeWidget()
        self.p_tree.setHeaderLabels(["Hash", "Subject", "PR", "Author", "Date"])
        self.p_tree.setRootIsDecorated(False)
        self.p_tree.setUniformRowHeights(True)
        self.p_tree.setAlternatingRowColors(True)
        hdr = self.p_tree.header()
        hdr.setStretchLastSection(False)
        for col, mode in ((P_SHA, QHeaderView.ResizeMode.ResizeToContents),
                          (P_SUBJECT, QHeaderView.ResizeMode.Stretch),
                          (P_PR, QHeaderView.ResizeMode.ResizeToContents),
                          (P_AUTHOR, QHeaderView.ResizeMode.Interactive),
                          (P_DATE, QHeaderView.ResizeMode.ResizeToContents)):
            hdr.setSectionResizeMode(col, mode)
        self.p_tree.setColumnWidth(P_AUTHOR, 140)
        self.p_tree.currentItemChanged.connect(self.on_promote_current_changed)
        self.p_stack.addWidget(self.p_tree)
        lv.addWidget(self.p_stack, 1)
        self.p_summary = QLabel()
        self.p_summary.setEnabled(False)
        lv.addWidget(self.p_summary)
        split.addWidget(left)
        self.p_diff = QPlainTextEdit()
        self.p_diff.setReadOnly(True)
        self.p_diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.p_diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.p_diff.setPlaceholderText("Select a commit to see its changes")
        self.p_highlighter = DiffHighlighter(self.p_diff.document())
        split.addWidget(self.p_diff)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        # merge conflict message
        m = self.p_conflict_msg = MessageWidget("warning")
        self.p_editor_btn = m.add_button(push_button("Open in Editor", self.open_conflicts_in_editor,
                                                     "Open the conflicted files", ("document-edit",)))
        m.add_button(push_button("Open Folder", self.open_folder, "Open the working copy in the file manager",
                                 ("document-open-folder", "folder-open")))
        m.add_button(push_button("Take Incoming", lambda: self.on_promote_resolve("theirs"),
                                 "Resolve every conflicted file with the version from the promoted branch",
                                 ("go-next",)))
        m.add_button(push_button("Keep Target", lambda: self.on_promote_resolve("ours"),
                                 "Resolve every conflicted file with the target branch version", ("go-previous",)))
        m.add_stretch()
        m.add_button(push_button("Abort Merge", self.on_promote_abort, "Give up on this merge",
                                 ("process-stop",)))
        self.p_commit_btn = m.add_button(push_button("Commit Merge", self.on_promote_commit,
                                                     "Commit the merge with the resolved files", ("dialog-ok-apply",)))
        self.p_conflict_btns = [m.buttons.itemAt(i).widget() for i in range(m.buttons.count())
                                if isinstance(m.buttons.itemAt(i).widget(), QPushButton)]
        root.addWidget(m)

        # result message
        r = self.p_result_msg = MessageWidget("positive", closable=True)
        r.add_button(push_button("Open Folder", self.open_folder, icons=("document-open-folder", "folder-open")))
        self.p_push_btn = r.add_button(push_button("Push", self.on_promote_push, "Push the branch to GitHub",
                                                   ("cloud-upload", "go-up")))
        self.p_pr_btn = r.add_button(push_button("Create Pull Request…", self.on_promote_pr,
                                                 "Open GitHub to create the pull request",
                                                 ("vcs-merge-request", "internet-web-browser")))
        r.add_stretch()
        root.addWidget(r)

        # bottom: branch, fork, push option, primary action
        row = QHBoxLayout()
        br_lbl = QLabel("New branch:")
        row.addWidget(br_lbl)
        self.p_branch_edit = QLineEdit()
        self.p_branch_edit.setMinimumWidth(300)
        self.p_branch_edit.setToolTip("Branch created from the target to hold the merge")
        self.p_branch_edit.textEdited.connect(lambda t: setattr(self, "p_branch_auto", not t.strip()))
        br_lbl.setBuddy(self.p_branch_edit)
        row.addWidget(self.p_branch_edit)
        row.addSpacing(spacing * 2)
        fork_lbl = QLabel("Push to fork:")
        row.addWidget(fork_lbl)
        self.p_fork_edit = QLineEdit()
        self.p_fork_edit.setMinimumWidth(220)
        self.p_fork_edit.setPlaceholderText("none: push to the repository")
        self.p_fork_edit.setToolTip("Your fork (owner/repo or git URL) to push the branch to. "
                                    "Leave empty to push to the loaded repository itself.")
        fork_lbl.setBuddy(self.p_fork_edit)
        row.addWidget(self.p_fork_edit)
        row.addSpacing(spacing * 2)
        self.p_push_cb = QCheckBox("Push when done")
        self.p_push_cb.setChecked(self.cfg.get("promote_push", False))
        row.addWidget(self.p_push_cb)
        row.addStretch(1)
        self.p_go_btn = push_button("Promote", self.on_promote,
                                    "Fetch, create the branch and merge (Ctrl+Return)",
                                    ("vcs-merge", "merge", "dialog-ok-apply"))
        row.addWidget(self.p_go_btn)
        root.addLayout(row)
        self.set_p_placeholder()

    def p_source(self):
        return self.p_src_combo.currentText().strip()

    def p_target(self):
        return self.p_dst_combo.currentText().strip()

    def set_p_placeholder(self, icons=("vcs-branch", "folder-git", "git"), title="No repository loaded",
                          explanation="Enter a GitHub repository above and press Load."):
        self.p_placeholder.set(icons, title, explanation)
        self.p_stack.setCurrentWidget(self.p_placeholder)

    def clear_promote(self):
        self.p_tree.clear()
        self.p_diff.clear()
        self.p_summary.setText("")
        self.p_total = 0
        self.p_go_btn.setText("Promote")
        self.update_sensitivity()

    def on_promote_branches_changed(self):
        self.p_branch_auto = True  # the suggested name follows the branches
        self.load_promote()

    def load_promote(self):
        if not self.repo:
            return
        src, dst = self.p_source(), self.p_target()
        branches = [self.p_src_combo.itemText(i) for i in range(self.p_src_combo.count())]
        if src not in branches or dst not in branches:
            return
        self.clear_promote()
        if self.p_branch_auto:
            self.p_branch_edit.setText(promote_branch_name(src, dst))
        if src == dst:
            self.set_p_placeholder(("vcs-branch", "dialog-information"), "Pick two different branches", "")
            return
        self.cfg.setdefault("repos", {}).setdefault(self.repo.spec, {}).setdefault("promote", {}).update(
            source=src, target=dst)
        save_config(self.cfg)
        self.set_p_placeholder(("view-history", "document-open-recent"), "Loading commits…", "")
        repo = self.repo

        def done(res):
            if (self.p_source(), self.p_target()) != (src, dst) or repo is not self.repo:
                return  # selection changed meanwhile
            commits, total, stat = res
            mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            for c in commits:
                pr = pr_from_subject(c["subject"])
                it = QTreeWidgetItem([c["short"], c["subject"], f"#{pr}" if pr else "", c["author"], c["date"]])
                it.commit = c
                it.setFont(P_SHA, mono)
                it.setToolTip(P_SUBJECT, c["subject"] + ("\nMerge commit" if c["merge"] else ""))
                self.p_tree.addTopLevelItem(it)
            self.p_total = total
            if total:
                txt = f"{total} commit{'s' if total != 1 else ''} on {src} not yet in {dst}"
                if total > len(commits):
                    txt += f" (newest {len(commits)} shown)"
                self.p_summary.setText(txt + (f" · {stat}" if stat else ""))
                self.p_go_btn.setText(f"Promote {total} Commit{'s' if total != 1 else ''}")
                self.p_stack.setCurrentWidget(self.p_tree)
            else:
                self.set_p_placeholder(("checkmark", "dialog-ok"), "Nothing to promote",
                                       f"{dst} already has everything on {src}.")
            self.update_sensitivity()
        self.run_bg(lambda: repo.promote_commits(dst, src), done,
                    lambda e: (self.set_p_placeholder(("dialog-error",), "Could not list commits", ""),
                               self.error(e)))

    def on_promote_current_changed(self, item, _prev):
        if item is None or not self.repo:
            return
        self.p_diff_req += 1
        req, repo, sha = self.p_diff_req, self.repo, item.commit["sha"]

        def work():
            try:
                text = repo.show(sha)
            except GitError as e:
                text = str(e)
            self.bridge.call.emit(lambda: req == self.p_diff_req and self.p_diff.setPlainText(text))
        threading.Thread(target=work, daemon=True).start()

    def on_promote(self):
        if not self.repo or self.busy or self.picking or not self.p_go_btn.isEnabled():
            return
        repo, src, dst = self.repo, self.p_source(), self.p_target()
        branch = self.p_branch_edit.text().strip() or promote_branch_name(src, dst)
        fork = self.p_fork_edit.text().strip()
        self.cfg.setdefault("repos", {}).setdefault(repo.spec, {}).setdefault("promote", {})["fork"] = fork
        self.cfg["promote_push"] = self.p_push_cb.isChecked()
        save_config(self.cfg)
        self.p_last = dict(source=src, target=dst, branch=branch, fork=fork)
        self.picking = True  # a git operation owns the working copy until it ends
        self.p_result_msg.hide()
        self.error_msg.hide()
        self.log(f"--- promoting origin/{src} into new branch {branch} (from origin/{dst})")
        self.set_status(f"Fetching, then merging {src} into {branch}…")

        def work():
            repo.fetch()
            repo.new_branch(dst, branch)
            return repo.merge(src)
        self.run_bg(work, self.after_promote_merge, lambda e: (self.end_picking(), self.error(e)))

    def after_promote_merge(self, result):
        if result == "conflict":
            self.run_bg(self.repo.conflicts, self.show_promote_conflict)
        else:
            self.finish_promote(result)

    def show_promote_conflict(self, files):
        self.conflict_files = files
        p = self.p_last
        self.set_status(f"Merge conflicts in {len(files)} file{'s' if len(files) != 1 else ''}")
        esc = html.escape
        listing = "<br>".join(f"&nbsp;&nbsp;•&nbsp;<tt>{esc(f)}</tt>" for f in files)
        self.p_conflict_msg.show_message(
            "warning",
            f"<b>Conflicts while merging {esc(p['source'])} into <tt>{esc(p['branch'])}</tt></b><br>{listing}<br>"
            "Resolve the conflicts and press <b>Commit Merge</b>, or settle every file one way with "
            f"<i>Take Incoming</i> ({esc(p['source'])}) or <i>Keep Target</i> ({esc(p['target'])}).")
        self.p_commit_btn.setDefault(True)
        self.p_commit_btn.setFocus()
        if self.auto_editor_act.isChecked():
            self.open_conflicts_in_editor()

    def on_promote_resolve(self, side):
        repo = self.repo
        self.run_bg(lambda: (repo.resolve_all(side), repo.commit_merge()), lambda _: self.finish_promote("ok"))

    def on_promote_commit(self):
        self.run_bg(self.repo.commit_merge, lambda _: self.finish_promote("ok"))

    def on_promote_abort(self):
        def done(_):
            self.end_picking()
            p = self.p_last
            self.p_result_msg.show_message(
                "information", f"Merge aborted. <tt>{html.escape(p['branch'])}</tt> is left at "
                               f"<tt>origin/{html.escape(p['target'])}</tt>.")
            self.p_push_btn.hide()
            self.p_pr_btn.hide()
            self.set_status("Merge aborted")
        self.run_bg(self.repo.abort_merge, done)

    def finish_promote(self, result):
        self.end_picking()
        self.p_conflict_msg.hide()
        p, esc = self.p_last, html.escape
        self.p_pr_btn.hide()
        if result == "uptodate":
            self.p_result_msg.show_message("information", f"Nothing to merge: {esc(p['target'])} already has "
                                                          f"everything on {esc(p['source'])}.")
            self.p_push_btn.hide()
            self.set_status("Already up to date")
            return
        self.p_result_template = (f"<b>Merged {esc(p['source'])}</b> into <tt>{esc(p['branch'])}</tt> "
                                  f"(%STATE%), ready for a pull request into <tt>{esc(p['target'])}</tt>.")
        self.p_result_msg.show_message("positive", self.p_result_template.replace("%STATE%", "local"))
        self.p_push_btn.show()
        self.p_branch_auto = True
        self.set_status("Merged")
        if self.p_push_cb.isChecked():
            self.on_promote_push()

    def on_promote_push(self):
        p, repo = self.p_last, self.repo
        if not p or not repo:
            return
        where = p["fork"] or "the repository"
        self.set_status(f"Pushing {p['branch']} to {where}…")

        def done(_):
            self.set_status(f"Pushed {p['branch']}")
            state = f"pushed to {html.escape(p['fork'])}" if p["fork"] else "pushed"
            self.p_result_msg.text.setText(self.p_result_template.replace("%STATE%", state))
            self.p_push_btn.hide()
            self.p_pr_btn.setVisible(repo.compare_url(p["target"], p["branch"], p["fork"]) is not None)
        self.run_bg(lambda: repo.push(p["branch"], p["fork"] or None), done)

    def on_promote_pr(self):
        p = self.p_last
        url = self.repo and p and self.repo.compare_url(p["target"], p["branch"], p["fork"])
        if url:
            QDesktopServices.openUrl(QUrl(url))


def run(initial_repo=None):
    app = QApplication([sys.argv[0]])
    app.setApplicationName("cherry-picker")
    app.setApplicationDisplayName(f"Cherry Picker {__version__}")  # keeps the version in the title bar
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("cherry-picker")  # lets Plasma match the window to the launcher and its icon
    app.setWindowIcon(icon("vcs-merge-request", "git", "applications-development"))
    win = Window(initial_repo)
    win.show()
    return app.exec()
