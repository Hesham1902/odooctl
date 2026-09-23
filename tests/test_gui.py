from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from odooctl import compose, gui, provision, pull, registry
from odooctl.cli import main


def _project(path):
    return {
        "path": str(path),
        "services": {"web": "web", "db": "db"},
        "ports": {"http": 8069, "pg_postgres": 5432},
    }


def test_load_project_status_without_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "get_projects", lambda: {"acme": _project(tmp_path)})
    monkeypatch.setattr(compose, "daemon_available", lambda: False)

    rows = gui.load_project_status()

    assert rows == [
        gui.ProjectStatus(
            "acme", str(tmp_path), "Unavailable", "8069", "5432", "Docker Desktop is not running"
        )
    ]


def test_load_project_status_reports_running_containers(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "get_projects", lambda: {"acme": _project(tmp_path)})
    monkeypatch.setattr(compose, "daemon_available", lambda: True)
    monkeypatch.setattr(compose, "ps", lambda path: [{"State": "running"}])

    assert gui.load_project_status()[0].state == "Running"


def test_run_project_action_uses_the_registered_web_service(monkeypatch, tmp_path):
    project = _project(tmp_path)
    monkeypatch.setattr(registry, "resolve", lambda slug: (slug, project))
    monkeypatch.setattr(compose, "daemon_available", lambda: True)

    class Result:
        stdout = b"container started"
        stderr = b""

    seen = {}

    def fake_run(path, *args, **kwargs):
        seen["path"] = path
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(compose, "run", fake_run)

    result = gui.run_project_action("acme", "restart")

    assert result == gui.ProjectActionResult("acme", "restart", "container started")
    assert seen == {
        "path": str(tmp_path),
        "args": ("restart", "web"),
        "kwargs": {"timeout": gui.ACTION_TIMEOUT},
    }


def test_run_project_action_requires_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "resolve", lambda slug: (slug, _project(tmp_path)))
    monkeypatch.setattr(compose, "daemon_available", lambda: False)

    with pytest.raises(compose.DockerError, match="Start Docker Desktop"):
        gui.run_project_action("acme", "logs")


def test_run_project_action_waits_for_odoo_after_start(monkeypatch, tmp_path):
    project = _project(tmp_path)
    monkeypatch.setattr(registry, "resolve", lambda slug: (slug, project))
    monkeypatch.setattr(compose, "daemon_available", lambda: True)
    monkeypatch.setattr(gui, "wait_http", lambda port, timeout: True)

    class Result:
        stdout = b"started"
        stderr = b""

    monkeypatch.setattr(compose, "run", lambda *args, **kwargs: Result())

    result = gui.run_project_action("acme", "up")

    assert "ready at http://localhost:8069" in result.output


def test_rescan_projects_returns_scan_diagnostics(monkeypatch, tmp_path):
    rows = (gui.ProjectStatus("acme", str(tmp_path), "Down", "8069", "5432", "No containers"),)
    report = SimpleNamespace(roots={str(tmp_path): 1}, rejected=[("bad.yml", "invalid")])
    seen = {}

    def fake_refresh_registry(roots):
        seen["roots"] = roots
        return {"roots": [str(tmp_path)]}, report

    monkeypatch.setattr(registry, "refresh_registry", fake_refresh_registry)
    monkeypatch.setattr(gui, "load_project_status", lambda: list(rows))

    result = gui.rescan_projects((str(tmp_path),))

    assert result == gui.ProjectScanResult(rows, (str(tmp_path),), 1)
    assert seen == {"roots": (str(tmp_path),)}


def test_open_project_uses_detected_http_port(monkeypatch, tmp_path):
    project = _project(tmp_path)
    monkeypatch.setattr(registry, "resolve", lambda slug: (slug, project))
    opened = []
    monkeypatch.setattr(gui.webbrowser, "open", opened.append)

    assert gui.open_project("acme") == ("acme", "http://localhost:8069")
    assert opened == ["http://localhost:8069"]


def test_gui_command_explains_optional_dependency(monkeypatch):
    monkeypatch.setattr(gui, "launch", lambda: (_ for _ in ()).throw(gui.GuiUnavailableError("install gui")))
    result = CliRunner().invoke(main, ["gui"])

    assert result.exit_code != 0
    assert "install gui" in result.output


def test_gui_command_defers_update_check_to_window(monkeypatch):
    monkeypatch.delenv("ODOOCTL_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(
        gui.update,
        "check_for_update",
        lambda: (_ for _ in ()).throw(AssertionError("CLI checked before opening the window")),
    )
    monkeypatch.setattr(gui, "launch", lambda: 0)
    result = CliRunner().invoke(main, ["gui"])
    assert result.exit_code == 0, result.output


def test_desktop_shows_update_without_blocking_project_refresh(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.delenv("ODOOCTL_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(gui.update, "check_for_update", lambda: "99.0.0")
    monkeypatch.setattr(gui, "load_project_status", lambda: [])

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QPushButton

    app = QApplication.instance() or QApplication([])
    observed = []
    finished = [False]

    def inspect():
        if finished[0]:
            return
        windows = [window for window in app.topLevelWidgets() if window.windowTitle() == "odooctl"]
        if windows:
            button = windows[0].findChild(QPushButton, "UpdateButton")
            if button and button.isVisible():
                observed.append(button.text())
                windows[0].close()
                app.quit()
                return
        QTimer.singleShot(10, inspect)

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(app.quit)
    QTimer.singleShot(0, inspect)
    timeout.start(3000)
    result = gui.launch()
    finished[0] = True
    timeout.stop()
    assert result == 0
    assert observed == ["odooctl 99.0.0 available · View release"]


def test_rewrite_compose_localizes_windows_home_path(tmp_path):
    data = {
        "services": {
            "web": {
                "ports": ["8056:8069"],
                "volumes": [r"C:\Users\dev\_odoo_addons\odoo-18.0\odoo\addons:/mnt/enterprise"],
            },
            "db": {"image": "postgres:16", "ports": ["5456:5432"]},
        }
    }

    rewritten, _, _ = provision.rewrite_compose(data, "acme", "18.0", lambda port: port + 1)

    enterprise = rewritten["services"]["web"]["volumes"][0]
    assert enterprise.startswith(Path.home().as_posix())
    assert "odoo-18.0" in enterprise


def test_missing_ssh_is_reported_as_a_pull_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(pull.subprocess, "run", missing)
    with pytest.raises(pull.PullError, match="ssh was not found"):
        pull.find_remote_backup("u@h")


def test_filestore_stream_uses_python_tar_extraction(monkeypatch, tmp_path):
    import io
    import tarfile

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        data = b"attachment"
        info = tarfile.TarInfo("home/odoo/data/filestore/db1/blob")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    archive.seek(0)

    class FakeProcess:
        stdout = archive

        def wait(self):
            return 0

    monkeypatch.setattr(pull.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    pull._stream_remote_tar("u@h", None, "/backup", "home/odoo/data", tmp_path, progress=False)

    assert (tmp_path / "home/odoo/data/filestore/db1/blob").read_bytes() == b"attachment"
