"""Optional desktop project manager backed by the odooctl registry."""

import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import __version__, compose, registry
from .commands.common import wait_http
from .pull_workflow import PullOptions, run_pull
from .runtime_env import prepare_gui_environment


class GuiUnavailableError(RuntimeError):
    """Raised when the optional GUI dependency is not installed."""


@dataclass(frozen=True)
class ProjectStatus:
    """Display data for one registered Odoo project."""

    slug: str
    path: str
    state: str
    http: str
    postgres: str
    detail: str


@dataclass(frozen=True)
class ProjectActionResult:
    """Output from an action run against one registered project."""

    slug: str
    action: str
    output: str


@dataclass(frozen=True)
class ProjectScanResult:
    """Result of rescanning configured roots for Odoo projects."""

    rows: tuple
    roots: tuple
    rejected: int


ACTION_TIMEOUT = 600
HTTP_READY_TIMEOUT = 120


def load_project_status():
    """Read registered projects and their Docker Compose state."""
    projects = registry.get_projects()
    docker_available = compose.daemon_available()
    rows = []
    for slug, project in sorted(projects.items()):
        ports = project.get("ports") or {}
        http = str(ports.get("http", "-"))
        postgres = str(ports.get("postgres") or ports.get("pg_postgres", "-"))
        if not docker_available:
            state, detail = "Unavailable", "Docker Desktop is not running"
        else:
            try:
                containers = compose.ps(project["path"])
                states = {(row.get("State") or "").lower() for row in containers}
                if not containers or states == {"exited"}:
                    state, detail = "Down", "No running containers"
                elif states == {"running"}:
                    state, detail = "Running", f"{len(containers)} container(s)"
                else:
                    state, detail = "Partial", f"{len(containers)} container(s)"
            except (compose.DockerError, KeyError) as exc:
                state, detail = "Error", str(exc)
        rows.append(ProjectStatus(slug, project.get("path", ""), state, http, postgres, detail))
    return rows


def _decode_output(process):
    """Combine captured process output into text suitable for the desktop log panel."""
    stdout = (process.stdout or b"").decode(errors="replace").strip()
    stderr = (process.stderr or b"").decode(errors="replace").strip()
    return "\n".join(value for value in (stdout, stderr) if value)


def run_project_action(slug, action):
    """Run a safe, non-destructive project action through Docker Compose."""
    resolved_slug, project = registry.resolve(slug)
    if not compose.daemon_available():
        raise compose.DockerError("Docker daemon not reachable. Start Docker Desktop and try again.")
    service = project["services"]["web"]
    commands = {
        "up": ("up", "-d"),
        "down": ("down",),
        "restart": ("restart", service),
        "logs": ("logs", "--tail=200", service),
    }
    try:
        args = commands[action]
    except KeyError as exc:
        raise ValueError(f"Unsupported GUI action: {action}") from exc
    process = compose.run(project["path"], *args, timeout=ACTION_TIMEOUT)
    output = _decode_output(process) or f"[{resolved_slug}] {action} complete."
    if action == "up":
        port = (project.get("ports") or {}).get("http")
        if port and wait_http(port, timeout=HTTP_READY_TIMEOUT):
            output = f"[{resolved_slug}] ready at http://localhost:{port}\n{output}"
        elif port:
            output = f"[{resolved_slug}] started but is not answering yet.\n{output}"
    return ProjectActionResult(resolved_slug, action, output)


def open_project(slug):
    """Open a project's detected HTTP port in the default browser."""
    resolved_slug, project = registry.resolve(slug)
    port = (project.get("ports") or {}).get("http")
    if not port:
        raise ValueError(f"No HTTP port detected for {resolved_slug}.")
    url = f"http://localhost:{port}"
    webbrowser.open(url)
    return resolved_slug, url


def rescan_projects(roots=()):
    """Rescan configured roots and return display rows plus scan diagnostics."""
    config, report = registry.refresh_registry(roots=roots)
    rows = tuple(load_project_status())
    return ProjectScanResult(rows, tuple(sorted(config.get("roots") or ())), len(report.rejected))


def pull_project(slug, options, progress=print):
    """Run the shared pull workflow for a registered project."""
    resolved_slug, project = registry.resolve(slug)
    return run_pull(resolved_slug, project, options, progress=progress)


# Background/foreground colors used for the project status badge, keyed by state.
STATUS_COLORS = {
    "Running": ("#d3f5df", "#1b7a3d"),
    "Down": ("#e9ecef", "#495057"),
    "Partial": ("#ffe8cc", "#b3590a"),
    "Unavailable": ("#ffe8cc", "#b3590a"),
    "Error": ("#ffe0e0", "#c1121f"),
    "default": ("#e9ecef", "#495057"),
}

# A small, cohesive Qt stylesheet applied once at the application level so widgets
# share consistent spacing, color, and typography instead of one-off inline styles.
APP_STYLESHEET = """
QWidget#CentralWidget, QMainWindow, QDialog {
    background-color: #f4f5f7;
}
QWidget {
    font-size: 13px;
    color: #1d2129;
}
QLabel#AppTitle {
    font-size: 17px;
    font-weight: 700;
}
QLabel#AppSubtitle {
    color: #6b7280;
    font-size: 12px;
}
QLabel#SectionLabel {
    color: #8a8f98;
    font-size: 11px;
    font-weight: 700;
}
QWidget#HeaderBar, QFrame#Card {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
}
QPushButton {
    background-color: #ffffff;
    border: 1px solid #d0d5dd;
    border-radius: 6px;
    padding: 6px 14px;
}
QPushButton:hover {
    background-color: #f1f3f5;
}
QPushButton:pressed {
    background-color: #e5e7eb;
}
QPushButton:disabled {
    color: #adb5bd;
    background-color: #f8f9fa;
    border-color: #e5e7eb;
}
QPushButton#PrimaryButton {
    background-color: #2f6fed;
    border: 1px solid #2f6fed;
    color: #ffffff;
    font-weight: 600;
    padding: 8px 18px;
}
QPushButton#PrimaryButton:hover {
    background-color: #2559c8;
}
QPushButton#PrimaryButton:disabled {
    background-color: #a9c2f5;
    border-color: #a9c2f5;
    color: #eef2ff;
}
QTableWidget {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: #e7effe;
    selection-color: #1d2129;
    alternate-background-color: #fafbfc;
}
QTableWidget::item {
    padding: 4px 6px;
}
QHeaderView::section {
    background-color: #f8f9fa;
    border: none;
    border-bottom: 1px solid #e5e7eb;
    padding: 6px;
    font-weight: 600;
    color: #495057;
}
QGroupBox {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #344054;
}
QLineEdit {
    border: 1px solid #d0d5dd;
    border-radius: 6px;
    padding: 6px 8px;
    background: #ffffff;
}
QLineEdit:focus {
    border: 1px solid #2f6fed;
}
QCheckBox#DangerCheck {
    color: #c1121f;
    font-weight: 600;
}
QPlainTextEdit {
    background-color: #0f172a;
    color: #d1d5db;
    border-radius: 8px;
    border: 1px solid #1f2937;
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    padding: 8px;
}
QToolButton#SectionToggle {
    border: none;
    background: transparent;
    font-weight: 600;
    color: #344054;
    padding: 4px 0;
}
QStatusBar {
    background-color: #f4f5f7;
    color: #6b7280;
    border-top: 1px solid #e5e7eb;
}
QSplitter::handle {
    background-color: #f4f5f7;
    width: 10px;
}
"""


def launch():
    """Open the optional desktop project manager and run its event loop."""
    prepare_gui_environment()
    try:
        from PySide6.QtCore import QObject, Qt, QThread, Signal
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QCheckBox,
            QDialog,
            QDialogButtonBox,
            QFileDialog,
            QFormLayout,
            QFrame,
            QGroupBox,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QSplitter,
            QStackedWidget,
            QTableWidget,
            QTableWidgetItem,
            QToolButton,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise GuiUnavailableError(
            "GUI support is not installed. Run `python -m pip install 'odooctl[gui]'` and try again."
        ) from exc

    class StatusBadge(QLabel):
        """A small colored pill that reflects one project's Docker Compose state."""

        def __init__(self, state="", parent=None):
            super().__init__(parent)
            self.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.setFixedHeight(20)
            self.set_state(state)

        def set_state(self, state):
            """Update the label text and colors for a new state."""
            self.setText(state or "-")
            bg, fg = STATUS_COLORS.get(state, STATUS_COLORS["default"])
            self.setStyleSheet(
                f"background-color: {bg}; color: {fg}; border-radius: 10px; "
                "padding: 1px 10px; font-weight: 600; font-size: 11px;"
            )

    class CollapsibleSection(QWidget):
        """A titled, collapsible group of secondary widgets for advanced options."""

        def __init__(self, title, expanded=False, parent=None):
            super().__init__(parent)
            self.toggle_button = QToolButton()
            self.toggle_button.setObjectName("SectionToggle")
            self.toggle_button.setText(title)
            self.toggle_button.setCheckable(True)
            self.toggle_button.setChecked(expanded)
            self.toggle_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            self.toggle_button.setArrowType(
                Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
            )
            self.toggle_button.clicked.connect(self._on_toggled)

            self.content = QWidget()
            self.content.setVisible(expanded)
            self.content_layout = QVBoxLayout(self.content)
            self.content_layout.setContentsMargins(22, 4, 0, 4)
            self.content_layout.setSpacing(6)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)
            layout.addWidget(self.toggle_button)
            layout.addWidget(self.content)

        def _on_toggled(self, checked):
            self.toggle_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
            self.content.setVisible(checked)

        def add_widget(self, widget):
            """Add one widget to the collapsible body."""
            self.content_layout.addWidget(widget)

    class PullDialog(QDialog):
        """A focused, single-purpose flow for pulling and restoring one backup."""

        def __init__(self, parent, slug, saved):
            super().__init__(parent)
            self.setWindowTitle(f"Pull backup · {slug}")
            self.setMinimumWidth(580)
            self.setModal(True)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 22, 24, 20)
            layout.setSpacing(14)

            heading = QLabel(f"Pull backup for <b>{slug}</b>")
            heading.setObjectName("AppTitle")
            layout.addWidget(heading)

            intro = QLabel(
                "Fetch the newest Odoo.sh backup over SSH, restore it locally, "
                "and choose which safety steps to run."
            )
            intro.setWordWrap(True)
            intro.setObjectName("AppSubtitle")
            layout.addWidget(intro)

            connection = QGroupBox("Connection")
            form = QFormLayout(connection)
            form.setSpacing(10)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
            self.target_edit = QLineEdit(saved.get("from", ""))
            self.target_edit.setPlaceholderText("ssh://1234567@your-project.odoo.com")
            form.addRow("SSH target", self.target_edit)
            self.path_edit = QLineEdit(saved.get("path", ""))
            self.path_edit.setPlaceholderText("Optional · newest standard backup by default")
            form.addRow("Remote path", self.path_edit)
            self.database_edit = QLineEdit(saved.get("db", ""))
            self.database_edit.setPlaceholderText(f"Optional · defaults to {slug}_pulled")
            form.addRow("Database name", self.database_edit)

            key_row = QWidget()
            key_layout = QHBoxLayout(key_row)
            key_layout.setContentsMargins(0, 0, 0, 0)
            key_layout.setSpacing(8)
            self.key_edit = QLineEdit(saved.get("key", ""))
            self.key_edit.setPlaceholderText("Optional · uses your SSH agent/default key")
            key_layout.addWidget(self.key_edit)
            key_button = QPushButton("Choose…")
            key_button.clicked.connect(self.choose_key)
            key_layout.addWidget(key_button)
            form.addRow("Private key", key_row)
            layout.addWidget(connection)

            restore = QGroupBox("What to restore")
            restore_layout = QVBoxLayout(restore)
            restore_layout.setSpacing(8)
            self.with_filestore = QCheckBox("Include filestore and attachments")
            self.with_filestore.setToolTip("Downloads a larger backup containing images and attachments.")
            restore_layout.addWidget(self.with_filestore)
            self.reset_admin = QCheckBox("Reset the main login to admin / admin")
            self.reset_admin.setChecked(True)
            self.reset_admin.setToolTip("Makes the restored database immediately accessible locally.")
            restore_layout.addWidget(self.reset_admin)
            self.sanitize = QCheckBox("Sanitize the database for local development")
            self.sanitize.setChecked(True)
            self.sanitize.setToolTip("Neutralizes Odoo, pauses crons, disables mail, and scrubs contacts.")
            restore_layout.addWidget(self.sanitize)
            layout.addWidget(restore)

            advanced = CollapsibleSection("Advanced options")
            self.fix_icons = QCheckBox("Repair missing menu icons")
            self.fix_icons.setChecked(True)
            self.fix_icons.setToolTip("Re-imports icons from addon sources when no filestore is restored.")
            advanced.add_widget(self.fix_icons)
            self.keep_download = QCheckBox("Keep the downloaded backup bundle")
            advanced.add_widget(self.keep_download)
            self.save_settings = QCheckBox("Remember these connection settings")
            self.save_settings.setChecked(True)
            advanced.add_widget(self.save_settings)
            layout.addWidget(advanced)

            danger = QGroupBox("Safety")
            danger_layout = QVBoxLayout(danger)
            danger_layout.setSpacing(6)
            self.overwrite = QCheckBox("Replace an existing database without asking")
            self.overwrite.setToolTip("This permanently drops the selected local database before restoring.")
            self.overwrite.setObjectName("DangerCheck")
            danger_layout.addWidget(self.overwrite)
            warning = QLabel("Safety defaults are enabled. Uncheck them only when you understand the effect.")
            warning.setWordWrap(True)
            warning.setObjectName("AppSubtitle")
            danger_layout.addWidget(warning)
            layout.addWidget(danger)

            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
            )
            ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            ok_button.setText("Pull backup")
            ok_button.setObjectName("PrimaryButton")
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

            self.target_edit.setFocus()

        def choose_key(self):
            """Choose a local SSH private key."""
            selected, _ = QFileDialog.getOpenFileName(self, "Choose SSH private key", str(Path.home()))
            if selected:
                self.key_edit.setText(selected)

        def accept(self):
            """Validate the minimum connection input before closing the dialog."""
            if not self.target_edit.text().strip():
                QMessageBox.warning(self, "SSH target required", "Enter the SSH address shown by Odoo.sh.")
                self.target_edit.setFocus()
                return
            key = self.key_edit.text().strip()
            if key and not Path(key).expanduser().is_file():
                QMessageBox.warning(self, "SSH key not found", f"The selected key does not exist:\n{key}")
                self.key_edit.setFocus()
                return
            super().accept()

        def pull_options(self):
            """Return the selected values as the shared pull workflow interface."""
            return PullOptions(
                ssh_target=self.target_edit.text().strip() or None,
                remote_path=self.path_edit.text().strip() or None,
                database=self.database_edit.text().strip() or None,
                ssh_key=self.key_edit.text().strip() or None,
                with_filestore=self.with_filestore.isChecked(),
                reset_admin=self.reset_admin.isChecked(),
                fix_icons=self.fix_icons.isChecked(),
                sanitize=self.sanitize.isChecked(),
                keep_download=self.keep_download.isChecked(),
                overwrite=self.overwrite.isChecked(),
                save_settings=self.save_settings.isChecked(),
            )

    class TaskWorker(QObject):
        finished = Signal(object)
        progress = Signal(str)

        def __init__(self, task, with_progress=False):
            super().__init__()
            self.task = task
            self.with_progress = with_progress

        def run(self):
            """Run a backend task outside the GUI thread."""
            try:
                if self.with_progress:
                    result = self.task(self.progress.emit)
                else:
                    result = self.task()
            except Exception as exc:  # the window should remain usable after a registry error
                result = exc
            self.finished.emit(result)

    class ProjectWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self._thread = None
            self._worker = None
            self._task_finished = None
            self._refresh_after_task = False
            self._rows_by_slug = {}
            self.setWindowTitle("odooctl")
            self.setMinimumSize(900, 500)

            central = QWidget()
            central.setObjectName("CentralWidget")
            root = QVBoxLayout(central)
            root.setContentsMargins(16, 16, 16, 12)
            root.setSpacing(12)
            root.addWidget(self._build_header())

            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.setChildrenCollapsible(False)
            splitter.addWidget(self._build_project_panel())
            splitter.addWidget(self._build_detail_panel())
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setSizes([320, 620])
            root.addWidget(splitter, 1)

            self.setCentralWidget(central)
            self.statusBar().showMessage("Ready")
            self.set_busy(False)

        def _build_header(self):
            """Build the compact title bar with project-wide (non-contextual) actions."""
            header = QWidget()
            header.setObjectName("HeaderBar")
            layout = QHBoxLayout(header)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(8)

            titles = QVBoxLayout()
            titles.setSpacing(0)
            title = QLabel("odooctl")
            title.setObjectName("AppTitle")
            subtitle = QLabel("Local Odoo project manager")
            subtitle.setObjectName("AppSubtitle")
            titles.addWidget(title)
            titles.addWidget(subtitle)
            layout.addLayout(titles)
            layout.addStretch()

            self.refresh_button = QPushButton("Refresh")
            self.refresh_button.clicked.connect(self.refresh)
            layout.addWidget(self.refresh_button)
            self.rescan_button = QPushButton("Rescan")
            self.rescan_button.clicked.connect(self.rescan)
            layout.addWidget(self.rescan_button)
            self.add_folder_button = QPushButton("Add Folder…")
            self.add_folder_button.clicked.connect(self.add_folder)
            layout.addWidget(self.add_folder_button)
            return header

        def _build_project_panel(self):
            """Build the left-hand list of registered projects."""
            panel = QWidget()
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(6)

            label = QLabel("PROJECTS")
            label.setObjectName("SectionLabel")
            layout.addWidget(label)

            self.table = QTableWidget(0, 3)
            self.table.setHorizontalHeaderLabels(["Project", "Status", "HTTP"])
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.table.verticalHeader().setVisible(False)
            self.table.setAlternatingRowColors(True)
            self.table.setShowGrid(False)
            self.table.horizontalHeader().setStretchLastSection(True)
            self.table.itemSelectionChanged.connect(self.selection_changed)
            layout.addWidget(self.table, 1)
            return panel

        def _build_detail_panel(self):
            """Build the right-hand stack: an empty state, or the selected project's detail."""
            self.detail_stack = QStackedWidget()
            self.detail_stack.addWidget(self._wrap_card(self._build_empty_state()))
            self.detail_stack.addWidget(self._wrap_card(self._build_detail_content()))
            return self.detail_stack

        def _wrap_card(self, inner):
            """Wrap a widget in a padded, white, rounded card."""
            card = QFrame()
            card.setObjectName("Card")
            layout = QVBoxLayout(card)
            layout.setContentsMargins(20, 20, 20, 20)
            layout.addWidget(inner)
            return card

        def _build_empty_state(self):
            """Build the placeholder shown when no project is selected."""
            empty = QWidget()
            layout = QVBoxLayout(empty)
            layout.addStretch()
            message = QLabel("Select a project to see its details and actions.")
            message.setAlignment(Qt.AlignmentFlag.AlignCenter)
            message.setObjectName("AppSubtitle")
            layout.addWidget(message)
            layout.addStretch()
            return empty

        def _build_detail_content(self):
            """Build the selected project's status, info, actions, and activity log."""
            content = QWidget()
            layout = QVBoxLayout(content)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(12)

            head = QHBoxLayout()
            self.detail_name = QLabel("")
            self.detail_name.setObjectName("AppTitle")
            head.addWidget(self.detail_name)
            self.detail_badge = StatusBadge()
            head.addWidget(self.detail_badge)
            head.addStretch()
            self.pull_button = QPushButton("Pull Backup…")
            self.pull_button.setObjectName("PrimaryButton")
            self.pull_button.clicked.connect(self.pull_selected)
            head.addWidget(self.pull_button)
            layout.addLayout(head)

            info = QFormLayout()
            info.setSpacing(6)
            self.detail_http = QLabel("-")
            info.addRow("HTTP port", self.detail_http)
            self.detail_postgres = QLabel("-")
            info.addRow("Postgres port", self.detail_postgres)
            self.detail_path = QLabel("-")
            self.detail_path.setObjectName("AppSubtitle")
            info.addRow("Path", self.detail_path)
            self.detail_note = QLabel("-")
            self.detail_note.setWordWrap(True)
            self.detail_note.setObjectName("AppSubtitle")
            info.addRow("Detail", self.detail_note)
            layout.addLayout(info)

            actions = QHBoxLayout()
            actions.setSpacing(8)
            self.action_buttons = {}
            for action in ("up", "down", "restart", "logs"):
                button = QPushButton(action.title())
                button.clicked.connect(lambda _checked=False, name=action: self.run_action(name))
                self.action_buttons[action] = button
                actions.addWidget(button)
            self.open_button = QPushButton("Open in Browser")
            self.open_button.clicked.connect(self.open_selected)
            actions.addWidget(self.open_button)
            actions.addStretch()
            layout.addLayout(actions)

            log_label = QLabel("ACTIVITY")
            log_label.setObjectName("SectionLabel")
            layout.addWidget(log_label)
            self.output = QPlainTextEdit()
            self.output.setReadOnly(True)
            self.output.setPlaceholderText("Select a project and run an action to see its output.")
            self.output.setMaximumBlockCount(2000)
            layout.addWidget(self.output, 1)
            return content

        def show_detail(self, slug):
            """Show the empty state or populate the detail card for one project."""
            row = self._rows_by_slug.get(slug) if slug else None
            if row is None:
                self.detail_stack.setCurrentIndex(0)
                return
            self.detail_stack.setCurrentIndex(1)
            self.detail_name.setText(row.slug)
            self.detail_badge.set_state(row.state)
            self.detail_http.setText(row.http)
            self.detail_postgres.setText(row.postgres)
            elided = self.detail_path.fontMetrics().elidedText(
                row.path, Qt.TextElideMode.ElideMiddle, 440
            )
            self.detail_path.setText(elided)
            self.detail_path.setToolTip(row.path)
            self.detail_note.setText(row.detail)

        def refresh(self):
            """Refresh project state without blocking the window."""
            self.start_task(load_project_status, self.show_rows, "Refreshing...")

        def rescan(self):
            """Rescan all configured roots and update the project registry."""
            self.start_task(rescan_projects, self.show_rescan_result, "Scanning project roots...")

        def add_folder(self):
            """Add a user-selected folder to the registry scan roots."""
            directory = QFileDialog.getExistingDirectory(self, "Choose an Odoo projects folder")
            if directory:
                self.start_task(
                    lambda: rescan_projects((directory,)),
                    self.show_rescan_result,
                    f"Scanning {directory}...",
                )

        def pull_selected(self):
            """Open the pull dialog and run the selected backup workflow."""
            slug = self.selected_slug()
            if not slug:
                return
            dialog = PullDialog(self, slug, registry.load_pull_settings(slug))
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            options = dialog.pull_options()
            self.output.clear()
            self.start_task(
                lambda progress: pull_project(slug, options, progress),
                self.show_pull_result,
                f"Pulling backup for {slug}...",
                progress=True,
            )

        def start_task(self, task, callback, status, progress=False):
            """Run one backend task in a worker thread and restore the UI afterwards."""
            if self._thread and self._thread.isRunning():
                return
            self.set_busy(True)
            self.statusBar().showMessage(status)
            self._task_finished = callback
            self._thread = QThread(self)
            self._worker = TaskWorker(task, with_progress=progress)
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.run)
            if progress:
                self._worker.progress.connect(self.show_task_progress)
            self._worker.finished.connect(self._task_done)
            self._worker.finished.connect(self._thread.quit)
            self._worker.finished.connect(self._worker.deleteLater)
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.finished.connect(self._refresh_finished)
            self._thread.start()

        def show_task_progress(self, message):
            """Append progress emitted by a long-running worker."""
            self.output.appendPlainText(message)

        def _task_done(self, result):
            """Deliver a worker result to the callback on the GUI thread."""
            if self._task_finished:
                self._task_finished(result)

        def set_busy(self, busy):
            """Enable only controls that are safe while no task is running."""
            self.refresh_button.setEnabled(not busy)
            self.rescan_button.setEnabled(not busy)
            self.add_folder_button.setEnabled(not busy)
            self.pull_button.setEnabled(not busy and self.selected_slug() is not None)
            has_selection = self.selected_slug() is not None
            for button in self.action_buttons.values():
                button.setEnabled(not busy and has_selection)
            self.open_button.setEnabled(not busy and has_selection)

        def selected_slug(self):
            """Return the selected project slug, if any."""
            row = self.table.currentRow()
            item = self.table.item(row, 0) if row >= 0 else None
            return item.text() if item else None

        def selection_changed(self):
            """Show the selected project's detail and enable its actions."""
            self.show_detail(self.selected_slug())
            self.set_busy(bool(self._thread and self._thread.isRunning()))

        def run_action(self, action):
            """Run a project action without freezing the window."""
            slug = self.selected_slug()
            if not slug:
                return
            if action == "down":
                answer = QMessageBox.question(
                    self,
                    "Stop project?",
                    f"Stop and remove the containers and network for {slug}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
            self.start_task(
                lambda: run_project_action(slug, action),
                self.show_action_result,
                f"Running {action} for {slug}...",
            )

        def show_action_result(self, result):
            """Display action output or its error in the output panel."""
            if isinstance(result, Exception):
                self.output.setPlainText(str(result))
                self.statusBar().showMessage("Action failed")
                return
            self.output.setPlainText(result.output)
            self.statusBar().showMessage(f"{result.slug}: {result.action} complete")
            if result.action != "logs":
                self._refresh_after_task = True

        def show_pull_result(self, result):
            """Display the pull workflow log or its error."""
            if isinstance(result, Exception):
                self.output.setPlainText(str(result))
                self.statusBar().showMessage("Pull failed")
                return
            self.output.setPlainText(result.output)
            self.statusBar().showMessage(f"{result.slug}: pull complete")
            self._refresh_after_task = True

        def open_selected(self):
            """Open the selected project's local HTTP URL."""
            slug = self.selected_slug()
            if not slug:
                return
            try:
                resolved_slug, url = open_project(slug)
            except Exception as exc:
                self.output.setPlainText(str(exc))
                self.statusBar().showMessage("Could not open project")
                return
            self.output.setPlainText(f"Opened {resolved_slug}: {url}")
            self.statusBar().showMessage(f"Opened {resolved_slug}")

        def show_rows(self, result):
            """Render refreshed project state or show the loading error."""
            if isinstance(result, Exception):
                self.output.setPlainText(str(result))
                self.statusBar().showMessage(str(result))
                return
            previous = self.selected_slug()
            self.table.setRowCount(0)
            self._rows_by_slug = {row.slug: row for row in result}
            for row in result:
                index = self.table.rowCount()
                self.table.insertRow(index)
                name_item = QTableWidgetItem(row.slug)
                name_item.setToolTip(row.path)
                self.table.setItem(index, 0, name_item)

                badge_cell = QWidget()
                badge_layout = QHBoxLayout(badge_cell)
                badge_layout.setContentsMargins(6, 2, 6, 2)
                badge_layout.addWidget(StatusBadge(row.state))
                badge_layout.addStretch()
                self.table.setCellWidget(index, 1, badge_cell)

                http_item = QTableWidgetItem(row.http)
                http_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(index, 2, http_item)
            self.statusBar().showMessage(f"{len(result)} project(s)")
            if not result:
                roots = "\n".join(f"  - {root}" for root in registry.default_roots())
                self.output.setPlainText(
                    "No Odoo projects found.\n\n"
                    "Use Rescan, or Add Folder to choose the parent directory that contains your projects.\n\n"
                    f"Default roots:\n{roots}"
                )
            if previous and previous in self._rows_by_slug:
                for r in range(self.table.rowCount()):
                    if self.table.item(r, 0).text() == previous:
                        self.table.selectRow(r)
                        break
            self.selection_changed()

        def show_rescan_result(self, result):
            """Render a rescan result and explain what the scanner checked."""
            if isinstance(result, Exception):
                self.output.setPlainText(str(result))
                self.statusBar().showMessage("Rescan failed")
                return
            self.show_rows(result.rows)
            roots = "\n".join(f"  - {root}" for root in result.roots)
            self.output.setPlainText(
                f"Scanned {len(result.roots)} root(s); found {len(result.rows)} project(s).\n"
                f"Rejected compose files: {result.rejected}\n\n{roots}"
            )
            self.statusBar().showMessage(f"Found {len(result.rows)} project(s)")

        def closeEvent(self, event):
            """Prevent closing while a Docker task is still running."""
            if self._thread and self._thread.isRunning():
                QMessageBox.information(
                    self,
                    "Action still running",
                    "Wait for the current Docker action to finish before closing odooctl.",
                )
                event.ignore()
                return
            event.accept()

        def _refresh_finished(self):
            self._task_finished = None
            self._worker = None
            self._thread = None
            self.set_busy(False)
            if self._refresh_after_task:
                self._refresh_after_task = False
                self.refresh()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("odooctl")
    app.setApplicationVersion(__version__)
    app.setStyleSheet(APP_STYLESHEET)
    window = ProjectWindow()
    window.show()
    window.refresh()
    return app.exec()
