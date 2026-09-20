from pathlib import Path

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


def test_gui_command_explains_optional_dependency(monkeypatch):
    monkeypatch.setattr(gui, "launch", lambda: (_ for _ in ()).throw(gui.GuiUnavailableError("install gui")))
    result = CliRunner().invoke(main, ["gui"])

    assert result.exit_code != 0
    assert "install gui" in result.output


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
