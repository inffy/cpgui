"""The Qt main window. Import this only after cherry_picker.pyside.activate() has run."""
import base64
import html
import shlex
import sys
import threading
from types import SimpleNamespace

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import (QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontDatabase, QIcon,
                           QKeySequence, QSyntaxHighlighter, QTextCharFormat)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QCompleter,
                               QDockWidget, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSplitter,
                               QStackedWidget, QStyle, QTabWidget, QToolBar, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import __version__
from .core import (COMMIT_LIMIT, GitError, PushRejected, Repo, branch_name, editor_args, editor_icon_path, guess_promote_target,
                   installed_editors, launch, load_config, parse_pr, pr_from_subject, promote_branch_name,
                   revert_branch_name, save_config)

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


class Sequence(SimpleNamespace):
    """A tab that runs commits through `git cherry-pick` / `git revert`: its op, widgets and last result."""
    branch = target = None
    template = ""


def message_buttons(msg):
    return [msg.buttons.itemAt(i).widget() for i in range(msg.buttons.count())
            if isinstance(msg.buttons.itemAt(i).widget(), QPushButton)]


def commit_tree(checkable):
    """Commit list: [check,] hash, subject, PR, author, date (COL_* when checkable, else shifted by one)."""
    tree = QTreeWidget()
    labels = ["Hash", "Subject", "PR", "Author", "Date"]
    tree.setHeaderLabels([""] + labels if checkable else labels)
    tree.setRootIsDecorated(False)
    tree.setUniformRowHeights(True)
    tree.setAlternatingRowColors(True)
    hdr = tree.header()
    hdr.setStretchLastSection(False)
    off = 0 if checkable else -1
    modes = {COL_CHECK: QHeaderView.ResizeMode.ResizeToContents, COL_SHA: QHeaderView.ResizeMode.ResizeToContents,
             COL_SUBJECT: QHeaderView.ResizeMode.Stretch, COL_PR: QHeaderView.ResizeMode.ResizeToContents,
             COL_AUTHOR: QHeaderView.ResizeMode.Interactive, COL_DATE: QHeaderView.ResizeMode.ResizeToContents}
    for col, mode in modes.items():
        if col + off >= 0:
            hdr.setSectionResizeMode(col + off, mode)
    tree.setColumnWidth(COL_AUTHOR + off, 140)
    return tree


def commit_item(c, checkable):
    pr = pr_from_subject(c["subject"])
    cols = [c["short"], c["subject"], f"#{pr}" if pr else "", c["author"], c["date"]]
    it = QTreeWidgetItem([""] + cols if checkable else cols)
    it.commit = c
    off = 0 if checkable else -1
    if checkable:
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(COL_CHECK, Qt.CheckState.Unchecked)
    it.setFont(COL_SHA + off, QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    it.setToolTip(COL_SUBJECT + off, c["subject"] + ("\nMerge commit: handled against its first parent"
                                                     if c["merge"] else ""))
    return it


def diff_view():
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    view.setPlaceholderText("Select a commit to see its changes")
    view.highlighter = DiffHighlighter(view.document())
    return view


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
        self.p_branch_auto = True
        self.p_total = 0  # commits the promote merge would bring in
        self.p_diff_req = 0
        self.p_last = {}  # source / target / branch / fork of the last promotion
        self.p_result_template = ""
        self.r_branch_auto = True
        self.cur = None  # Sequence of the cherry-pick / revert in progress or last run
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
        self.push_btn = r.add_button(push_button("Push", lambda: self.on_push(self.seq_pick), "Push the branch to GitHub",
                                                 ("cloud-upload", "go-up")))
        self.pr_btn = r.add_button(push_button("Create Pull Request…", lambda: self.on_open_pr(self.seq_pick),
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
        self.seq_pick = Sequence(op="cherry-pick", ing="picking", ed="picked", record_cb=self.x_cb,
                                 sides="<i>Take Incoming</i> or <i>Keep Target</i>",
                                 conflict_msg=self.conflict_msg, continue_btn=self.continue_btn,
                                 result_msg=self.result_msg, push_btn=self.push_btn, pr_btn=self.pr_btn,
                                 push_cb=self.push_cb, reset_branch_name=lambda: setattr(self, "branch_auto", True))

        self.build_promote_tab(spacing)
        self.build_revert_tab(spacing)
        self.build_rebase_tab()

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
        for seq, fn in (("Ctrl+Return", self.on_primary), (QKeySequence.StandardKey.Find, self.on_find)):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)

        self.pr_timer = QTimer(self, singleShot=True, interval=250)
        self.pr_timer.timeout.connect(self.lookup_prs)

        self.controls = [self.repo_combo, self.load_act, self.fetch_act, self.src_combo, self.dst_combo,
                         self.swap_btn, self.merges_act, self.branch_edit,
                         self.p_src_combo, self.p_dst_combo, self.p_branch_edit, self.p_fork_edit,
                         self.r_branch_combo, self.r_branch_edit, self.b_pr_edit, self.b_lookup_btn]
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
            for key, split in (("promote_split", self.p_split), ("revert_split", self.r_split),
                               ("rebase_split", self.b_split)):
                if ui.get(key):
                    split.restoreState(base64.b64decode(ui[key]))
                else:
                    split.setSizes([650, 650])
            self.tabs.setCurrentIndex(int(ui.get("tab", 0)))
        except (ValueError, TypeError):
            self.resize(1300, 850)

    def closeEvent(self, event):
        enc = lambda b: base64.b64encode(bytes(b)).decode()  # noqa: E731
        self.cfg["ui"] = {"geometry": enc(self.saveGeometry()), "state": enc(self.saveState()),
                          "split": enc(self.split.saveState()), "promote_split": enc(self.p_split.saveState()),
                          "revert_split": enc(self.r_split.saveState()), "rebase_split": enc(self.b_split.saveState()),
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
        self.r_go_btn.setEnabled(idle and self.repo is not None and bool(self.r_selected()))
        pr = self.b_pr
        self.b_go_btn.setEnabled(idle and self.repo is not None and pr is not None
                                 and pr["state"] == "open" and pr["behind"] > 0)
        for b in self.r_conflict_btns + self.b_conflict_btns:
            b.setEnabled(self.busy == 0)
        self.seq_revert.push_btn.setEnabled(idle)
        self.b_push_btn.setEnabled(idle)
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
        self.clear_reverts()
        self.seq_revert.result_msg.hide()
        self.set_r_placeholder(("download", "folder-download"), "Loading repository…")
        self.clear_rebase()
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
        self.set_r_placeholder(("dialog-error",), "Could not load the repository")
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

    def fill_revert_branches(self, branches, value):
        cb = self.r_branch_combo
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
        r_branch = prefs.get("revert", {}).get("branch")
        self.fill_revert_branches(branches, r_branch if r_branch in branches else default)
        self.remember_recent(repo.spec)
        self.update_sensitivity()
        self.load_commits()
        self.load_promote()
        self.load_reverts()

    def on_refresh(self):
        if not self.repo:
            return self.on_load()
        if self.busy or self.picking:
            return
        repo, src, dst = self.repo, self.source(), self.target()
        p_src, p_dst = self.p_source(), self.p_target()
        r_branch = self.r_branch_combo.currentText().strip()
        self.set_status("Fetching…")

        def done(res):
            branches, _ = res
            self.fill_branches(branches, src, dst)
            self.fill_branches(branches, p_src, p_dst, (self.p_src_combo, self.p_dst_combo))
            self.fill_revert_branches(branches, r_branch)
            self.set_status(f"{len(branches)} branches")
            self.load_commits()
            self.load_promote()
            self.load_reverts()
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
        for b in (self.editor_btn, self.p_editor_btn, self.r_editor_btn, self.b_editor_btn):
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

    # ------------------------------------------------------------ cherry-picking / reverting
    # Both run a queue of commits through `git cherry-pick` or `git revert`, one at a time, stopping on
    # conflicts. `self.cur` is the Sequence of the tab doing it (its op, widgets and last result).
    def on_primary(self):  # Ctrl+Return
        {self.promote_tab: self.on_promote, self.revert_tab: self.on_revert,
         self.rebase_tab: self.on_rebase}.get(self.tabs.currentWidget(), self.on_go)()

    def on_find(self):  # Ctrl+F
        (self.r_search if self.tabs.currentWidget() is self.revert_tab else self.search).setFocus()

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
        self.start_sequence(self.seq_pick, commits, target, branch)

    def start_sequence(self, seq, commits, target, branch):
        self.cur = seq
        self.queue, self.qi = commits, 0
        self.stats = {"ok": [], "dropped": [], "skipped": []}
        seq.branch, seq.target = branch, target
        self.picking = True
        seq.result_msg.hide()
        self.error_msg.hide()
        self.log(f"--- {seq.ing} {len(commits)} commit(s) on new branch {branch} (from origin/{target})")
        self.set_status(f"Creating {branch}…")
        self.run_bg(lambda: self.repo.new_branch(target, branch), lambda _: self.pick_next(),
                    lambda e: (self.end_picking(), self.error(e)))

    def pick_next(self):
        if self.qi >= len(self.queue):
            return self.finish()
        c, seq = self.queue[self.qi], self.cur
        self.set_status(f"{seq.ing.capitalize()} {self.qi + 1} of {len(self.queue)}: {c['short']} {c['subject']}")
        record = seq.record_cb is not None and seq.record_cb.isChecked()
        self.run_bg(lambda: self.repo.pick(c["sha"], record, c["merge"], seq.op), self.after_pick,
                    lambda e: (self.finish(stopped=True), self.error(e)))

    def after_pick(self, result):
        c = self.queue[self.qi]
        if result == "conflict":
            self.run_bg(self.repo.conflicts, self.show_conflict)
            return
        self.stats[result].append(c)
        if result == "dropped":
            self.log(f"{c['short']}: nothing left to change on the branch - nothing to commit, skipped")
        self.qi += 1
        self.pick_next()

    def show_conflict(self, files):
        self.conflict_files = files
        c, seq = self.queue[self.qi], self.cur
        self.set_status(f"Conflict in {c['short']}")
        esc = html.escape
        listing = "<br>".join(f"&nbsp;&nbsp;•&nbsp;<tt>{esc(f)}</tt>" for f in files)
        seq.conflict_msg.show_message(
            "warning",
            f"<b>Conflict while {seq.ing} {c['short']} “{esc(c['subject'])}”</b> "
            f"({self.qi + 1} of {len(self.queue)})<br>{listing}<br>"
            f"Resolve the conflicts and press <b>Continue</b>, or settle every file one way with {seq.sides}.")
        seq.continue_btn.setDefault(True)
        seq.continue_btn.setFocus()
        if self.auto_editor_act.isChecked():
            self.open_conflicts_in_editor()

    def on_resolve(self, side):
        repo, op = self.repo, self.cur.op
        self.run_bg(lambda: (repo.resolve_all(side), repo.continue_pick(op))[1], self.after_resolve)

    def on_continue(self):
        repo, op = self.repo, self.cur.op
        self.run_bg(lambda: repo.continue_pick(op), self.after_resolve)

    def after_resolve(self, result):
        self.cur.conflict_msg.hide()
        self.after_pick(result)

    def on_skip(self):
        def done(_):
            self.cur.conflict_msg.hide()
            self.stats["skipped"].append(self.queue[self.qi])
            self.qi += 1
            self.pick_next()
        repo, op = self.repo, self.cur.op
        self.run_bg(lambda: repo.skip(op), done)

    def on_stop(self):
        repo, op = self.repo, self.cur.op
        self.run_bg(lambda: repo.abort(op), lambda _: self.finish(stopped=True))

    def end_picking(self):
        self.picking = False
        if self.cur:
            self.cur.conflict_msg.hide()
        self.update_sensitivity()

    def finish(self, stopped=False):
        self.end_picking()
        seq = self.cur
        ok, dropped, skipped = self.stats["ok"], self.stats["dropped"], self.stats["skipped"]
        parts = [f"<b>{len(ok)} commit{'s' if len(ok) != 1 else ''} {seq.ed}</b> on "
                 f"<tt>{html.escape(seq.branch)}</tt> (%STATE%)"]
        if dropped:
            parts.append(f"{len(dropped)} with nothing to change")
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        if stopped:
            parts.append(f"stopped with {len(self.queue) - self.qi} not {seq.ed}")
        seq.template = ", ".join(parts) + "."
        kind = "positive" if ok and not stopped else "information"
        seq.result_msg.show_message(kind, seq.template.replace("%STATE%", "local"))
        self.set_status("Stopped" if stopped else "Done")
        seq.push_btn.setVisible(bool(ok))
        seq.pr_btn.hide()
        seq.reset_branch_name()
        if ok and seq.push_cb.isChecked():
            self.on_push(seq)

    def on_push(self, seq):
        branch = seq.branch
        if not branch or not self.repo:
            return
        repo = self.repo
        self.set_status(f"Pushing {branch}…")

        def done(_):
            self.set_status(f"Pushed {branch}")
            seq.result_msg.text.setText(seq.template.replace("%STATE%", "pushed"))
            seq.push_btn.hide()
            seq.pr_btn.setVisible(repo.compare_url(seq.target, branch) is not None)
        self.push_branch(repo, branch, None, done)

    def push_branch(self, repo, branch, fork, done):
        """Push; if the branch already exists there with other commits, ask before replacing it."""
        def rejected(e):
            if not isinstance(e, PushRejected):
                return self.error(e)
            where = fork or repo.slug or "the repository"
            box = QMessageBox(QMessageBox.Icon.Question, "Replace Branch",
                              f"“{branch}” already exists on {where} with different commits, "
                              "probably from an earlier run.", parent=self)
            box.setInformativeText("Replace it with the new branch? Its current commits are dropped from it, "
                                   "and an open pull request from it will show the new ones instead.")
            replace = box.addButton("Replace Branch", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is not replace:
                self.set_status(f"Not pushed: {branch} already exists on {where}")
                return
            self.set_status(f"Replacing {branch} on {where}…")
            self.run_bg(lambda: repo.push(branch, fork, replace=True), done)
        self.run_bg(lambda: repo.push(branch, fork), done, rejected)

    def on_open_pr(self, seq):
        url = self.repo and self.repo.compare_url(seq.target, seq.branch)
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # ------------------------------------------------------------ revert tab
    def build_revert_tab(self, spacing):
        tab = self.revert_tab = QWidget()
        i = self.tabs.addTab(tab, icon("edit-undo", "document-revert"), "Revert")
        self.tabs.setTabToolTip(i, "Undo chosen commits on a branch with new revert commits")
        root = QVBoxLayout(tab)

        row = QHBoxLayout()
        lbl = QLabel("Revert on:")
        row.addWidget(lbl)
        self.r_branch_combo = searchable_combo()
        self.r_branch_combo.setToolTip("Branch that has the commits you want to undo")
        lbl.setBuddy(self.r_branch_combo)
        self.r_branch_combo.activated.connect(lambda _i: self.load_reverts())
        self.r_branch_combo.lineEdit().returnPressed.connect(self.load_reverts)
        row.addWidget(self.r_branch_combo)
        self.r_search = QLineEdit()
        self.r_search.setPlaceholderText("Filter…")
        self.r_search.setToolTip("Filter by message, author, hash or #PR (Ctrl+F)")
        self.r_search.setClearButtonEnabled(True)
        self.r_search.addAction(icon("search", "edit-find"), QLineEdit.ActionPosition.LeadingPosition)
        self.r_search.textChanged.connect(lambda _t: self.r_apply_filter())
        row.addWidget(self.r_search, 1)
        row.addWidget(push_button("Deselect All", lambda: self.r_select_none(), "Clear the selection",
                                  ("edit-select-none",)))
        root.addLayout(row)

        split = self.r_split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.r_stack = QStackedWidget()
        self.r_placeholder = Placeholder()
        self.r_stack.addWidget(self.r_placeholder)
        self.r_tree = commit_tree(checkable=True)
        self.r_tree.itemChanged.connect(lambda _it, col: col == COL_CHECK and self.r_selection_changed())
        self.r_tree.itemActivated.connect(self.toggle_item)
        self.r_tree.currentItemChanged.connect(lambda it, _p: self.show_diff(it, self.r_diff))
        self.r_stack.addWidget(self.r_tree)
        lv.addWidget(self.r_stack, 1)
        self.r_count_lbl = QLabel()
        self.r_count_lbl.setEnabled(False)
        lv.addWidget(self.r_count_lbl)
        split.addWidget(left)
        self.r_diff = diff_view()
        split.addWidget(self.r_diff)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        seq = self.seq_revert = Sequence(op="revert", ing="reverting", ed="reverted", record_cb=None,
                                         sides="<i>Take Revert</i> or <i>Keep Branch</i>",
                                         reset_branch_name=lambda: setattr(self, "r_branch_auto", True))
        m = seq.conflict_msg = MessageWidget("warning")
        self.r_editor_btn = m.add_button(push_button("Open in Editor", self.open_conflicts_in_editor,
                                                     "Open the conflicted files", ("document-edit",)))
        m.add_button(push_button("Open Folder", self.open_folder, "Open the working copy in the file manager",
                                 ("document-open-folder", "folder-open")))
        m.add_button(push_button("Take Revert", lambda: self.on_resolve("theirs"),
                                 "Resolve every conflicted file as the revert has it", ("go-previous",)))
        m.add_button(push_button("Keep Branch", lambda: self.on_resolve("ours"),
                                 "Resolve every conflicted file with the branch's current version",
                                 ("go-next",)))
        m.add_stretch()
        m.add_button(push_button("Skip Commit", self.on_skip, "Leave this commit unreverted and go on",
                                 ("media-skip-forward",)))
        m.add_button(push_button("Stop", self.on_stop, "Abort this revert and stop; earlier reverts stay",
                                 ("process-stop",)))
        seq.continue_btn = m.add_button(push_button("Continue", self.on_continue,
                                                    "Commit the resolved files and go on", ("dialog-ok-apply",)))
        self.r_conflict_btns = message_buttons(m)
        root.addWidget(m)
        r = seq.result_msg = MessageWidget("positive", closable=True)
        r.add_button(push_button("Open Folder", self.open_folder, icons=("document-open-folder", "folder-open")))
        seq.push_btn = r.add_button(push_button("Push", lambda: self.on_push(self.seq_revert),
                                                "Push the branch to GitHub", ("cloud-upload", "go-up")))
        seq.pr_btn = r.add_button(push_button("Create Pull Request…", lambda: self.on_open_pr(self.seq_revert),
                                              "Open GitHub to create the pull request",
                                              ("vcs-merge-request", "internet-web-browser")))
        r.add_stretch()
        root.addWidget(r)

        row = QHBoxLayout()
        br_lbl = QLabel("New branch:")
        row.addWidget(br_lbl)
        self.r_branch_edit = QLineEdit()
        self.r_branch_edit.setMinimumWidth(300)
        self.r_branch_edit.setToolTip("Branch created from the chosen branch to hold the reverts")
        self.r_branch_edit.textEdited.connect(lambda t: setattr(self, "r_branch_auto", not t.strip()))
        br_lbl.setBuddy(self.r_branch_edit)
        row.addWidget(self.r_branch_edit)
        row.addSpacing(spacing * 2)
        seq.push_cb = QCheckBox("Push when done")
        seq.push_cb.setChecked(self.cfg.get("revert_push", False))
        row.addWidget(seq.push_cb)
        row.addStretch(1)
        self.r_go_btn = push_button("Revert", self.on_revert, "Revert the selected commits (Ctrl+Return)",
                                    ("edit-undo", "dialog-ok-apply"))
        row.addWidget(self.r_go_btn)
        root.addLayout(row)
        self.set_r_placeholder()

    def r_items(self):
        return [self.r_tree.topLevelItem(i) for i in range(self.r_tree.topLevelItemCount())]

    def r_selected(self):
        return [it.commit for it in self.r_items() if it.checkState(COL_CHECK) == Qt.CheckState.Checked]

    def set_r_placeholder(self, icons=("vcs-branch", "folder-git", "git"), title="No repository loaded",
                          explanation="Enter a GitHub repository above and press Load."):
        self.r_placeholder.set(icons, title, explanation)
        self.r_stack.setCurrentWidget(self.r_placeholder)

    def clear_reverts(self):
        self.r_tree.clear()
        self.r_diff.clear()
        self.r_selection_changed()

    def r_selection_changed(self):
        sel = self.r_selected()
        n, items = len(sel), self.r_items()
        hidden = sum(it.isHidden() for it in items)
        txt = f"{len(items)} commits" if items else ""
        if hidden:
            txt += f", {hidden} hidden"
        if len(items) >= COMMIT_LIMIT:
            txt += f" (newest {COMMIT_LIMIT} shown)"
        if n:
            txt += f" · {n} selected"
        self.r_count_lbl.setText(txt)
        self.r_go_btn.setText(f"Revert {n} Commit{'s' if n != 1 else ''}" if n else "Revert")
        branch = self.r_branch_combo.currentText().strip()
        if self.r_branch_auto and branch:
            self.r_branch_edit.setText(revert_branch_name(branch, sel))
        self.update_sensitivity()

    def r_apply_filter(self):
        q = self.r_search.text().strip().lower()
        for it in self.r_items():
            c = it.commit
            pr = pr_from_subject(c["subject"])
            it.setHidden(bool(q) and not (q in c["subject"].lower() or q in c["author"].lower()
                                          or c["sha"].startswith(q) or (pr is not None and q.lstrip("#") == str(pr))))
        self.r_selection_changed()

    def r_select_none(self):
        self.r_tree.blockSignals(True)
        for it in self.r_items():
            it.setCheckState(COL_CHECK, Qt.CheckState.Unchecked)
        self.r_tree.blockSignals(False)
        self.r_tree.viewport().update()
        self.r_selection_changed()

    def load_reverts(self):
        if not self.repo:
            return
        branch = self.r_branch_combo.currentText().strip()
        if branch not in [self.r_branch_combo.itemText(i) for i in range(self.r_branch_combo.count())]:
            return
        self.r_branch_auto = True
        self.clear_reverts()
        self.cfg.setdefault("repos", {}).setdefault(self.repo.spec, {})["revert"] = {"branch": branch}
        save_config(self.cfg)
        self.set_r_placeholder(("view-history", "document-open-recent"), "Loading commits…", "")
        repo = self.repo

        def done(commits):
            if self.r_branch_combo.currentText().strip() != branch or repo is not self.repo:
                return
            self.r_tree.blockSignals(True)
            for c in commits:
                self.r_tree.addTopLevelItem(commit_item(c, checkable=True))
            self.r_tree.blockSignals(False)
            self.r_apply_filter()
            if commits:
                self.r_stack.setCurrentWidget(self.r_tree)
            else:
                self.set_r_placeholder(("dialog-information",), "No commits on this branch", "")
        self.run_bg(lambda: repo.branch_commits(branch), done,
                    lambda e: (self.set_r_placeholder(("dialog-error",), "Could not list commits", ""), self.error(e)))

    def on_revert(self):
        commits = self.r_selected()  # newest first, which is the order to undo them in
        if not commits or not self.repo or self.busy or self.picking:
            return
        branch = self.r_branch_combo.currentText().strip()
        new = self.r_branch_edit.text().strip() or revert_branch_name(branch, commits)
        self.cfg["revert_push"] = self.seq_revert.push_cb.isChecked()
        save_config(self.cfg)
        self.start_sequence(self.seq_revert, commits, branch, new)

    # ------------------------------------------------------------ rebase PR tab
    def build_rebase_tab(self):
        tab = self.rebase_tab = QWidget()
        i = self.tabs.addTab(tab, icon("vcs-pull", "go-up", "view-refresh"), "Rebase PR")
        self.tabs.setTabToolTip(i, "Rebase a pull request onto the latest version of its base branch")
        root = QVBoxLayout(tab)

        row = QHBoxLayout()
        lbl = QLabel("Pull request:")
        row.addWidget(lbl)
        self.b_pr_edit = QLineEdit()
        self.b_pr_edit.setPlaceholderText("123, #123 or its GitHub URL")
        self.b_pr_edit.setMinimumWidth(240)
        self.b_pr_edit.returnPressed.connect(self.on_rebase_lookup)
        lbl.setBuddy(self.b_pr_edit)
        row.addWidget(self.b_pr_edit)
        self.b_lookup_btn = push_button("Look Up", self.on_rebase_lookup, "Fetch the pull request and its commits",
                                        ("edit-find", "search"))
        row.addWidget(self.b_lookup_btn)
        row.addStretch(1)
        root.addLayout(row)
        self.b_info = QLabel()
        self.b_info.setWordWrap(True)
        self.b_info.setTextFormat(Qt.TextFormat.RichText)
        self.b_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.b_info.setOpenExternalLinks(True)
        self.b_info.hide()
        root.addWidget(self.b_info)
        self.b_note = MessageWidget("information")
        root.addWidget(self.b_note)

        split = self.b_split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.b_stack = QStackedWidget()
        self.b_placeholder = Placeholder()
        self.b_stack.addWidget(self.b_placeholder)
        self.b_tree = commit_tree(checkable=False)
        self.b_tree.currentItemChanged.connect(lambda it, _p: self.show_diff(it, self.b_diff))
        self.b_stack.addWidget(self.b_tree)
        split.addWidget(self.b_stack)
        self.b_diff = diff_view()
        split.addWidget(self.b_diff)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        m = self.b_conflict_msg = MessageWidget("warning")
        self.b_editor_btn = m.add_button(push_button("Open in Editor", self.open_conflicts_in_editor,
                                                     "Open the conflicted files", ("document-edit",)))
        m.add_button(push_button("Open Folder", self.open_folder, "Open the working copy in the file manager",
                                 ("document-open-folder", "folder-open")))
        m.add_button(push_button("Take PR Version", lambda: self.on_rebase_resolve("theirs"),
                                 "Resolve every conflicted file with the pull request's version", ("go-next",)))
        m.add_button(push_button("Keep Base", lambda: self.on_rebase_resolve("ours"),
                                 "Resolve every conflicted file with the base branch version", ("go-previous",)))
        m.add_stretch()
        m.add_button(push_button("Skip Commit", lambda: self.rebase_cmd(lambda r: r.rebase_skip()),
                                 "Drop this commit from the pull request and go on", ("media-skip-forward",)))
        m.add_button(push_button("Abort Rebase", self.on_rebase_abort, "Give up and leave the pull request as it is",
                                 ("process-stop",)))
        self.b_continue_btn = m.add_button(push_button("Continue", lambda: self.rebase_cmd(lambda r: r.rebase_continue()),
                                                       "Commit the resolved files and go on", ("dialog-ok-apply",)))
        self.b_conflict_btns = message_buttons(m)
        root.addWidget(m)
        r = self.b_result_msg = MessageWidget("positive", closable=True)
        r.add_button(push_button("Open Folder", self.open_folder, icons=("document-open-folder", "folder-open")))
        self.b_push_btn = r.add_button(push_button("Force Push", self.on_rebase_push,
                                                   "Replace the pull request's commits with the rebased ones",
                                                   ("cloud-upload", "go-up")))
        r.add_button(push_button("Open Pull Request", self.on_rebase_open, icons=("internet-web-browser",)))
        r.add_stretch()
        root.addWidget(r)

        row = QHBoxLayout()
        row.addStretch(1)
        self.b_go_btn = push_button("Rebase", self.on_rebase, "Rebase the pull request onto its base (Ctrl+Return)",
                                    ("vcs-pull", "go-up", "dialog-ok-apply"))
        row.addWidget(self.b_go_btn)
        root.addLayout(row)
        self.reset_rebase_view()

    def clear_rebase(self):
        self.reset_rebase_view()
        self.update_sensitivity()

    def reset_rebase_view(self):
        self.b_pr = None
        self.b_tree.clear()
        self.b_diff.clear()
        self.b_info.hide()
        self.b_note.hide()
        self.b_result_msg.hide()
        self.b_go_btn.setText("Rebase")
        self.b_placeholder.set(("vcs-merge-request", "vcs-pull"), "No pull request",
                               "Enter a pull request number above and press Look Up.")
        self.b_stack.setCurrentWidget(self.b_placeholder)

    def b_can_push(self):
        pr = self.b_pr
        return bool(pr) and (pr["same_repo"] or (pr["can_modify"] and pr["head_repo"] is not None))

    def on_rebase_lookup(self):
        if not self.repo or self.busy or self.picking:
            return
        try:
            n = parse_pr(self.b_pr_edit.text())
        except GitError as e:
            return self.error(e)
        self.clear_rebase()
        self.error_msg.hide()
        self.b_placeholder.set(("view-history", "document-open-recent"), f"Looking up #{n}…", "")
        self.set_status(f"Fetching pull request #{n}…")
        repo = self.repo
        self.run_bg(lambda: repo.pr_info(n), self.show_pr_info,
                    lambda e: (self.clear_rebase(), self.set_status(""), self.error(e)))

    def show_pr_info(self, pr):
        self.b_pr = pr
        esc = html.escape
        head = f"{pr['head_repo'] or '(deleted fork)'}:{pr['head']}"
        n = len(pr["commits"])
        self.b_info.setText(
            f"<b><a href=\"{esc(pr['url'])}\">#{pr['number']}</a> {esc(pr['title'])}</b>"
            f"{' (draft)' if pr['draft'] else ''}<br>"
            f"by {esc(pr['author'])} · <tt>{esc(head)}</tt> → <tt>{esc(pr['base'])}</tt> · "
            f"{n} commit{'s' if n != 1 else ''} · {pr['behind']} commit{'s' if pr['behind'] != 1 else ''} "
            f"behind {esc(pr['base'])}")
        self.b_info.show()
        notes = []
        kind = "information"
        if pr["state"] != "open":
            notes.append(f"This pull request is {pr['state']}.")
            kind = "warning"
        elif pr["behind"] == 0:
            notes.append(f"It is already up to date with {esc(pr['base'])}; there is nothing to rebase.")
        if not self.b_can_push():
            kind = "warning"
            notes.append("You can rebase it here, but not push it back: "
                         + ("its fork has been deleted." if pr["head_repo"] is None else
                            "it comes from a fork whose author hasn't allowed edits from maintainers. "
                            "Ask them to tick “Allow edits from maintainers”, or to rebase it themselves."))
        if notes:
            self.b_note.show_message(kind, " ".join(notes))
        for c in pr["commits"]:
            self.b_tree.addTopLevelItem(commit_item(c, checkable=False))
        self.b_stack.setCurrentWidget(self.b_tree)
        self.b_go_btn.setText(f"Rebase onto {pr['base']}")
        self.set_status(f"#{pr['number']}: {n} commits, {pr['behind']} behind {pr['base']}")
        self.update_sensitivity()

    def on_rebase(self):
        pr, repo = self.b_pr, self.repo
        if not pr or not repo or self.busy or self.picking or not self.b_go_btn.isEnabled():
            return
        self.picking = True
        self.b_result_msg.hide()
        self.error_msg.hide()
        self.log(f"--- rebasing #{pr['number']} ({pr['head']}) onto origin/{pr['base']}")
        self.set_status(f"Rebasing #{pr['number']} onto {pr['base']}…")
        self.run_bg(lambda: repo.start_rebase(pr), self.after_rebase,
                    lambda e: (self.end_picking(), self.error(e)))

    def rebase_cmd(self, fn):
        repo = self.repo
        self.run_bg(lambda: fn(repo), self.after_rebase)

    def on_rebase_resolve(self, side):
        self.rebase_cmd(lambda r: (r.resolve_all(side), r.rebase_continue())[1])

    def after_rebase(self, result):
        if result == "conflict":
            repo = self.repo
            self.run_bg(lambda: (repo.conflicts(), repo.rebase_step()), self.show_rebase_conflict)
        else:
            self.finish_rebase(result)

    def show_rebase_conflict(self, res):
        files, (pos, total, short, subject) = res
        self.conflict_files = files
        esc = html.escape
        self.set_status(f"Conflict in {short}")
        listing = "<br>".join(f"&nbsp;&nbsp;•&nbsp;<tt>{esc(f)}</tt>" for f in files)
        step = f" ({pos} of {total})" if total else ""
        self.b_conflict_msg.show_message(
            "warning",
            f"<b>Conflict while rebasing {esc(short)} “{esc(subject)}”</b>{step}<br>{listing}<br>"
            "Resolve the conflicts and press <b>Continue</b>, or settle every file one way with "
            "<i>Take PR Version</i> or <i>Keep Base</i>.")
        self.b_continue_btn.setDefault(True)
        self.b_continue_btn.setFocus()
        if self.auto_editor_act.isChecked():
            self.open_conflicts_in_editor()

    def on_rebase_abort(self):
        def done(_):
            self.end_picking()
            self.b_conflict_msg.hide()
            self.b_result_msg.show_message("information", "Rebase aborted. The pull request is unchanged.")
            self.b_push_btn.hide()
            self.set_status("Rebase aborted")
        repo = self.repo
        self.run_bg(repo.rebase_abort, done)

    def finish_rebase(self, result):
        self.end_picking()
        self.b_conflict_msg.hide()
        pr, esc = self.b_pr, html.escape
        if result == "uptodate":
            self.b_result_msg.show_message("information", f"Nothing to do: #{pr['number']} is already based on the "
                                                          f"latest {esc(pr['base'])}.")
            self.b_push_btn.hide()
            self.set_status("Already up to date")
            return
        self.b_result_msg.show_message(
            "positive", f"<b>Rebased #{pr['number']}</b> onto the latest <tt>{esc(pr['base'])}</tt> (local)."
            + ("" if self.b_can_push() else " It can't be pushed back to the pull request (see above)."))
        self.b_push_btn.setText(f"Force Push to {pr['head']}")
        self.b_push_btn.setVisible(self.b_can_push())
        self.set_status("Rebased")

    def on_rebase_push(self):
        pr, repo = self.b_pr, self.repo
        if not pr or not repo:
            return
        where = f"{pr['head_repo']}:{pr['head']}"
        answer = QMessageBox.question(
            self, "Force Push",
            f"Replace the commits of pull request #{pr['number']} with the rebased ones?\n\n"
            f"This force-pushes to {where}. It is refused if anyone pushed to the pull request "
            "after you looked it up.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.set_status(f"Force-pushing to {where}…")

        def done(_):
            self.set_status(f"Pushed #{pr['number']}")
            self.b_result_msg.text.setText(f"<b>Rebased #{pr['number']}</b> onto the latest "
                                           f"<tt>{html.escape(pr['base'])}</tt> and pushed.")
            self.b_push_btn.hide()
        self.run_bg(lambda: repo.push_pr(pr), done)

    def on_rebase_open(self):
        if self.b_pr:
            QDesktopServices.openUrl(QUrl(self.b_pr["url"]))

    # ------------------------------------------------------------ shared commit view helpers
    def show_diff(self, item, view):
        if item is None or not self.repo:
            return
        self.diff_req += 1
        req, repo, sha = self.diff_req, self.repo, item.commit["sha"]

        def work():
            try:
                text = repo.show(sha)
            except GitError as e:
                text = str(e)
            self.bridge.call.emit(lambda: req == self.diff_req and view.setPlainText(text))
        threading.Thread(target=work, daemon=True).start()

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
        self.push_branch(repo, p["branch"], p["fork"] or None, done)

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
