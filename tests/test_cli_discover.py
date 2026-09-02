import json

from click.testing import CliRunner
from conftest import write_compose

from odooctl import cli, registry


def _config():
    return json.loads(registry._config_file().read_text())


def _run(*args):
    return CliRunner().invoke(cli.main, ["discover", *args])


def test_discover_explains_an_empty_result(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    work = tmp_path / "work"
    (work / "webapp").mkdir(parents=True)
    (work / "webapp" / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx\n")

    result = _run("--root", str(tmp_path / "missing"), "--root", str(work))

    assert result.exit_code == 1
    out = result.output
    assert "No Odoo projects found." in out
    assert "Scan roots:" in out
    assert "missing" in out
    assert "scanned, 1 compose file" in out
    assert "(cwd, not saved)" in out
    assert "no Odoo web service" in out
    assert "1 compose file was found but none looked like an Odoo project" in out


def test_discover_hint_when_no_compose_files_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)

    result = _run()

    assert result.exit_code == 1
    assert "no compose files (compose.yaml / compose.yml" in result.output
    assert "up to 3 folders deep" in result.output


def test_discover_hint_when_every_root_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    monkeypatch.chdir(tmp_path)  # cwd itself scanned; make it the only existing root
    result = _run("--root", str(tmp_path / "gone"))
    assert result.exit_code == 1
    # cwd exists, so the "none exist" hint does not fire; the "no compose files" one does
    assert "gone" in result.output and "missing" in result.output


def test_discover_forget_root_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "work"
    write_compose(work / "proj")

    result = _run("--root", str(work) + "/")
    assert result.exit_code == 0, result.output
    assert _config()["roots"] == [str(work.resolve())]
    assert "Found 1 project(s):" in result.output

    result = _run("--forget-root", str(work))
    assert _config()["roots"] == []


def test_discover_verbose_prints_report_and_skips_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "work"
    write_compose(work / "proj")
    (work / "nginx").mkdir()
    (work / "nginx" / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx\n")

    quiet = _run("--root", str(work))
    assert quiet.exit_code == 0, quiet.output
    assert "1 compose file skipped - run `odooctl discover -v`" in quiet.output
    assert "Scan roots:" not in quiet.output

    loud = _run("-v")
    assert loud.exit_code == 0, loud.output
    assert "Scan roots:" in loud.output
    assert "no Odoo web service" in loud.output
    assert "skipped - run" not in loud.output


def test_discover_finds_project_in_cwd_without_saving_it(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    proj = write_compose(tmp_path / "agt_projects" / "wooden")
    monkeypatch.chdir(proj)

    result = _run()

    assert result.exit_code == 0, result.output
    assert "acme" in result.output
    assert f"keep it with: odooctl discover --root {proj.resolve()}" in result.output
    assert _config()["roots"] == []
    assert _config()["projects"]["acme"]["path"] == str(proj.resolve())
