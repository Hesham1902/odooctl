from pathlib import Path

from click.testing import CliRunner

from odooctl import cli, registry, testing, vcs


def _register(slug, tmp_path):
    d = tmp_path / slug
    d.mkdir()
    addons = d / "custom_addons"
    addons.mkdir()
    registry.register(slug, {
        "compose_file": str(d / "docker-compose.yml"),
        "path": str(d),
        "services": {"web": "web", "db": "db"},
        "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
        "ports": {"http": 8069},
        "custom_addons": str(addons),
        "db_user": "odoo",
    })
    return registry.get_projects()[slug]


def test_modules_from_files_maps_paths():
    files = [
        "custom_addons/sale_flow/models/order.py",
        "custom_addons/sale_flow/tests/test_x.py",
        "custom_addons/other/__manifest__.py",
        "docker-compose.yml",
        "/etc/passwd",
    ]
    mods = vcs.modules_from_files(files, "/proj/custom_addons")
    assert mods == ["other", "sale_flow"]


def test_changed_modules_uses_git_output(tmp_path, monkeypatch):
    (tmp_path / "custom_addons").mkdir()
    monkeypatch.setattr(vcs, "_git", lambda *a: "custom_addons/alpha/m.py\ncore/x.py\n")
    mods = vcs.changed_modules(tmp_path, tmp_path / "custom_addons")
    assert mods == ["alpha"]


def test_upgrade_changed_runs_combined_dash_u(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: False)
    monkeypatch.setattr(cli.vcs, "changed_modules", lambda p, a, ref=None: ["alpha", "beta"])

    cmds = []
    monkeypatch.setattr(cli.compose, "live_output",
                        lambda path, *a: cmds.append(a) or 0)

    result = CliRunner().invoke(
        cli.main, ["upgrade", "acme", "--changed", "-d", "prod"])
    assert result.exit_code == 0, result.output
    run_args = cmds[0]
    joined = run_args[run_args.index("-u") + 1]
    assert joined == "alpha,beta"
    assert entry["path"]


def test_upgrade_rejects_module_and_changed(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    result = CliRunner().invoke(
        cli.main, ["upgrade", "acme", "foo", "--changed", "-d", "prod"])
    assert result.exit_code != 0
    assert "not both" in result.output


def test_upgrade_no_changed_modules_errors(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.vcs, "changed_modules", lambda p, a, ref=None: [])
    result = CliRunner().invoke(
        cli.main, ["upgrade", "acme", "--changed", "-d", "prod"])
    assert result.exit_code != 0
    assert "No changed custom addons" in result.output


def test_test_changed_runs_each_module(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.vcs, "changed_modules", lambda p, a, ref=None: ["alpha", "beta"])

    calls = []

    def fake_run_tests(path, ent, module, db, **kw):
        calls.append((module, db))
        return testing.TestResult(ok=True, ran=2, failures=[], log_path=None, raw_tail="")

    monkeypatch.setattr(cli.testing, "run_tests", fake_run_tests)
    result = CliRunner().invoke(cli.main, ["test", "acme", "--changed"])
    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["alpha", "beta"]
    assert "2 passed" in result.output
    assert Path(entry["path"]).exists()


def test_git_error_surfaces_cleanly(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    def boom(*a, **kw):
        raise vcs.GitError("not a git repository")

    monkeypatch.setattr(cli.vcs, "changed_modules", boom)
    result = CliRunner().invoke(cli.main, ["test", "acme", "--changed"])
    assert result.exit_code != 0
    assert "git failed" in result.output
