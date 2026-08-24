
from pathlib import Path

from click.testing import CliRunner

from odooctl import cli, registry, testing


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


def _make_module(addons_dir, name, installable=True):
    mod = Path(addons_dir) / name
    mod.mkdir(parents=True)
    (mod / "__manifest__.py").write_text(f"{{'name': '{name}', 'installable': {installable}}}")


def _ok_result(ran=5):
    return testing.TestResult(ok=True, ran=ran, failures=[], log_path=None, raw_tail="")


def _fail_result():
    return testing.TestResult(ok=False, ran=3, failures=["FAIL: test_x"],
                              log_path=None, raw_tail="boom")


def test_test_requires_module_or_all(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    result = CliRunner().invoke(cli.main, ["test", "acme"])
    assert result.exit_code != 0
    assert "--all" in result.output


def test_test_rejects_all_and_module_together(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    result = CliRunner().invoke(cli.main, ["test", "acme", "foo", "--all"])
    assert result.exit_code != 0
    assert "only one of" in result.output


def test_test_all_runs_each_module_and_summarizes(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    addons = entry["custom_addons"]
    _make_module(addons, "alpha")
    _make_module(addons, "beta")
    _make_module(addons, "broken_mod", installable=False)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    calls = []

    def fake_run_tests(path, ent, module, db, **kw):
        calls.append((module, db))
        return _ok_result(7) if module == "alpha" else _fail_result()

    monkeypatch.setattr(cli.testing, "run_tests", fake_run_tests)

    result = CliRunner().invoke(cli.main, ["test", "acme", "--all"])
    assert result.exit_code == 1  # beta failed
    assert [c[0] for c in calls] == ["alpha", "beta"]
    assert all(c[1].startswith("test_") for c in calls)
    assert "skipping not-installable: broken_mod" in result.output
    assert "1 passed, 1 failed" in result.output
    assert "PASS" in result.output and "FAIL" in result.output


def test_test_all_stop_on_fail_halts(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    addons = entry["custom_addons"]
    _make_module(addons, "aaa_first")
    _make_module(addons, "zzz_last")

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    def fake_run_tests(path, ent, module, db, **kw):
        if module == "aaa_first":
            return _fail_result()
        raise AssertionError("should not run after failure")

    monkeypatch.setattr(cli.testing, "run_tests", fake_run_tests)
    result = CliRunner().invoke(cli.main, ["test", "acme", "--all", "-x"])
    assert result.exit_code == 1
    assert "stopping on first failure" in result.output
    assert "not run" in result.output


def test_test_single_module_still_works(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    _make_module(entry["custom_addons"], "solo")

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    seen = {}

    def fake_run_tests(path, ent, module, db, **kw):
        seen.update(module=module, db=db)
        return _ok_result()

    monkeypatch.setattr(cli.testing, "run_tests", fake_run_tests)
    result = CliRunner().invoke(
        cli.main, ["test", "acme", "solo", "-d", "custom_db", "--keep-db"])
    assert result.exit_code == 0, result.output
    assert seen == {"module": "solo", "db": "custom_db"}
