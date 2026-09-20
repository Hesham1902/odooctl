"""Optional desktop project manager backed by the odooctl registry."""

import sys
import webbrowser
from dataclasses import dataclass

from . import __version__, compose, registry
from .commands.common import wait_http
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


def launch():
    """Open the optional desktop project manager and run its event loop."""
    prepare_gui_environment()
    try:
        from PySide6.QtCore import QObject, QThread, Signal
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QFileDialog,
            QHBoxLayout,
            QMainWindow,
            QMessageBox,
            QPlainTextEdit,
            QPushButton,
            QTableWidget,
            QTableWidgetItem,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise GuiUnavailableError(
            "GUI support is not installed. Run `python -m pip install 'odooctl[gui]'` and try again."
        ) from exc

    class TaskWorker(QObject):
        finished = Signal(object)

        def __init__(self, task):
            super().__init__()
            self.task = task

        def run(self):
            """Run a backend task outside the GUI thread."""
            try:
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
            self.setWindowTitle("odooctl")
            self.setMinimumSize(900, 500)

            central = QWidget()
            layout = QVBoxLayout(central)
            toolbar = QHBoxLayout()
            self.refresh_button = QPushButton("Refresh")
            self.refresh_button.clicked.connect(self.refresh)
            toolbar.addWidget(self.refresh_button)
            self.rescan_button = QPushButton("Rescan")
            self.rescan_button.clicked.connect(self.rescan)
            toolbar.addWidget(self.rescan_button)
            self.add_folder_button = QPushButton("Add Folder")
            self.add_folder_button.clicked.connect(self.add_folder)
            toolbar.addWidget(self.add_folder_button)
            self.action_buttons = {}
            for action in ("up", "down", "restart", "logs"):
                button = QPushButton(action.title())
                button.clicked.connect(lambda _checked=False, name=action: self.run_action(name))
                button.setEnabled(False)
                self.action_buttons[action] = button
                toolbar.addWidget(button)
            self.open_button = QPushButton("Open")
            self.open_button.clicked.connect(self.open_selected)
            self.open_button.setEnabled(False)
            toolbar.addWidget(self.open_button)
            toolbar.addStretch()
            layout.addLayout(toolbar)

            self.table = QTableWidget(0, 6)
            self.table.setHorizontalHeaderLabels(["Project", "Status", "HTTP", "Postgres", "Path", "Details"])
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.itemSelectionChanged.connect(self.selection_changed)
            self.table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(self.table)

            self.output = QPlainTextEdit()
            self.output.setReadOnly(True)
            self.output.setPlaceholderText("Select a project and run an action to see its output.")
            self.output.setMaximumBlockCount(2000)
            layout.addWidget(self.output)
            self.setCentralWidget(central)
            self.statusBar().showMessage("Ready")

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

        def start_task(self, task, callback, status):
            """Run one backend task in a worker thread and restore the UI afterwards."""
            if self._thread and self._thread.isRunning():
                return
            self.set_busy(True)
            self.statusBar().showMessage(status)
            self._task_finished = callback
            self._thread = QThread(self)
            self._worker = TaskWorker(task)
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.run)
            self._worker.finished.connect(self._task_done)
            self._worker.finished.connect(self._thread.quit)
            self._worker.finished.connect(self._worker.deleteLater)
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.finished.connect(self._refresh_finished)
            self._thread.start()

        def _task_done(self, result):
            """Deliver a worker result to the callback on the GUI thread."""
            if self._task_finished:
                self._task_finished(result)

        def set_busy(self, busy):
            """Enable only controls that are safe while no task is running."""
            self.refresh_button.setEnabled(not busy)
            self.rescan_button.setEnabled(not busy)
            self.add_folder_button.setEnabled(not busy)
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
            """Enable project actions after a table row is selected."""
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
            self.table.setRowCount(0)
            colors = {
                "Running": "#2f9e44",
                "Down": "#868e96",
                "Partial": "#f08c00",
                "Unavailable": "#f08c00",
                "Error": "#e03131",
            }
            for row in result:
                index = self.table.rowCount()
                self.table.insertRow(index)
                values = [row.slug, row.state, row.http, row.postgres, row.path, row.detail]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 1:
                        item.setForeground(QColor(colors.get(row.state, "#212529")))
                    self.table.setItem(index, column, item)
            self.statusBar().showMessage(f"{len(result)} project(s)")
            if not result:
                roots = "\n".join(f"  - {root}" for root in registry.default_roots())
                self.output.setPlainText(
                    "No Odoo projects found.\n\n"
                    "Use Rescan, or Add Folder to choose the parent directory that contains your projects.\n\n"
                    f"Default roots:\n{roots}"
                )
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
    window = ProjectWindow()
    window.show()
    window.refresh()
    return app.exec()
