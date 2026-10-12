from types import SimpleNamespace

from click.testing import CliRunner

from odooctl import doctor, registry
from odooctl.cli import main


def _commands_work(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_run", lambda command: (True, "available"))


def test_doctor_reports_missing_docker_without_writing_config(isolated_config, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "[FAIL] Docker: docker is not on PATH" in result.output
    assert "Install Docker Desktop" in result.output
    assert "[WARN] Projects: No projects registered" in result.output
    assert not (isolated_config / "config.json").exists()
    assert not (isolated_config / "update-check.json").exists()


def test_doctor_validates_registered_compose_file(isolated_config, tmp_path, monkeypatch):
    _commands_work(monkeypatch)
    project = tmp_path / "acme"
    project.mkdir()
    compose_file = project / "docker-compose.yml"
    compose_file.write_text("services: {}")
    registry.save_config(
        {
            "roots": [str(tmp_path)],
            "projects": {"acme": {"path": str(project), "compose_file": str(compose_file)}},
        }
    )
    monkeypatch.setattr(
        doctor.compose,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr=b"invalid compose file\n", stdout=b""),
    )

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "[FAIL] Project acme: invalid compose file" in result.output
    assert f"Fix {compose_file}" in result.output


def test_doctor_finds_moved_project_and_required_ssh_tools(isolated_config, tmp_path, monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(doctor, "_run", lambda command: (True, "available"))
    registry.save_config(
        {
            "projects": {"acme": {"path": str(tmp_path / "moved"), "compose_file": "missing"}},
            "pull": {"acme": {"from": "ssh://user@example.com"}},
        }
    )

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "[FAIL] Project acme: Folder missing" in result.output
    assert "Run `odooctl discover`" in result.output
    assert "[FAIL] SSH tools: Missing: ssh, scp" in result.output


def test_doctor_reports_release_feed_failure(isolated_config, monkeypatch):
    _commands_work(monkeypatch)
    monkeypatch.delenv("ODOOCTL_NO_UPDATE_CHECK", raising=False)

    def unreachable():
        raise OSError("TLS verification failed")

    monkeypatch.setattr(doctor.update, "_fetch_latest_version", unreachable)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "[FAIL] Updates: Cannot reach the release feed: TLS verification failed" in result.output
    assert "Check HTTPS access" in result.output


def test_doctor_passes_and_flags_available_release(isolated_config, tmp_path, monkeypatch):
    _commands_work(monkeypatch)
    project = tmp_path / "acme"
    project.mkdir()
    compose_file = project / "docker-compose.yml"
    compose_file.write_text("services: {}")
    registry.save_config(
        {
            "roots": [str(tmp_path)],
            "projects": {"acme": {"path": str(project), "compose_file": str(compose_file)}},
        }
    )
    monkeypatch.setattr(
        doctor.compose,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr=b"", stdout=b""),
    )
    monkeypatch.delenv("ODOOCTL_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(doctor.update, "_fetch_latest_version", lambda: "99.0.0")

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "[PASS] Project acme: Compose config valid" in result.output
    assert "[WARN] Updates: odooctl 99.0.0 is available" in result.output
    assert doctor.update.release_url("99.0.0") in result.output
    assert "0 failed" in result.output


def test_doctor_flags_registered_path_with_wrong_letter_case(isolated_config, tmp_path, monkeypatch):
    _commands_work(monkeypatch)
    project = tmp_path / "Work" / "acme"
    project.mkdir(parents=True)
    (project / "docker-compose.yml").write_text("services: {}")
    wrong = tmp_path / "work" / "acme"
    monkeypatch.setattr(doctor.provision, "canonical_path", lambda p: project)
    registry.save_config(
        {
            "roots": [str(tmp_path)],
            "projects": {"acme": {"path": str(wrong), "compose_file": str(wrong / "docker-compose.yml")}},
        }
    )
    monkeypatch.setattr(doctor.Path, "is_dir", lambda self: True)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 1
    assert "differs in letter case" in result.output
    assert f"odooctl discover --root {project.parent}" in result.output
