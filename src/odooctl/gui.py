"""Optional desktop project manager backed by the odooctl registry."""

from dataclasses import dataclass

from . import compose, registry
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


def launch():
    """Open the optional desktop project manager and run its event loop."""
    prepare_gui_environment()
    try:
        from PySide6.QtCore import QObject, QThread, Signal
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import (
            QAbstractItemView,
            QApplication,
            QHBoxLayout,
            QMainWindow,
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

    class RefreshWorker(QObject):
        finished = Signal(object)

        def run(self):
            """Load status outside the GUI thread."""
            try:
                result = load_project_status()
            except Exception as exc:  # the window should remain usable after a registry error
                result = exc
            self.finished.emit(result)

    class ProjectWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self._thread = None
            self._worker = None
            self.setWindowTitle("odooctl")
            self.setMinimumSize(900, 500)

            central = QWidget()
            layout = QVBoxLayout(central)
            toolbar = QHBoxLayout()
            self.refresh_button = QPushButton("Refresh")
            self.refresh_button.clicked.connect(self.refresh)
            toolbar.addWidget(self.refresh_button)
            toolbar.addStretch()
            layout.addLayout(toolbar)

            self.table = QTableWidget(0, 6)
            self.table.setHorizontalHeaderLabels(["Project", "Status", "HTTP", "Postgres", "Path", "Details"])
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.horizontalHeader().setStretchLastSection(True)
            layout.addWidget(self.table)
            self.setCentralWidget(central)
            self.statusBar().showMessage("Ready")

        def refresh(self):
            """Refresh project state without blocking the window."""
            if self._thread and self._thread.isRunning():
                return
            self.refresh_button.setEnabled(False)
            self.statusBar().showMessage("Refreshing...")
            self._thread = QThread(self)
            self._worker = RefreshWorker()
            self._worker.moveToThread(self._thread)
            self._thread.started.connect(self._worker.run)
            self._worker.finished.connect(self.show_rows)
            self._worker.finished.connect(self._thread.quit)
            self._worker.finished.connect(self._worker.deleteLater)
            self._thread.finished.connect(self._thread.deleteLater)
            self._thread.finished.connect(self._refresh_finished)
            self._thread.start()

        def show_rows(self, result):
            """Render refreshed project state or show the loading error."""
            if isinstance(result, Exception):
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

        def _refresh_finished(self):
            self.refresh_button.setEnabled(True)
            self._worker = None
            self._thread = None

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("odooctl")
    window = ProjectWindow()
    window.show()
    window.refresh()
    return app.exec()
