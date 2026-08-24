from pathlib import Path

from click.testing import CliRunner

from odooctl import cli, registry, watcher


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


def test_snapshot_matches_extension_and_ignores_others(tmp_path):
    (tmp_path / "mod_a").mkdir()
    (tmp_path / "mod_a" / "x.py").write_text("a = 1")
    (tmp_path / "mod_b").mkdir()
    (tmp_path / "mod_b" / "__manifest__.py").write_text("{}")
    (tmp_path / "mod_b" / "readme.md").write_text("nope")
    sig = watcher.snapshot(tmp_path)
    assert len(sig) == 2
    assert all(p.endswith(".py") for p in sig)


def test_diff_files_reports_all_kinds(tmp_path):
    old = {"a.py": 1, "b.py": 1}
    new = {"a.py": 2, "c.py": 1}          # modified + created; b.py deleted
    assert watcher.diff_files(old, new) == ["a.py", "b.py", "c.py"]


def test_watch_restarts_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    mod = tmp_path / "mod_x"
    mod.mkdir()
    f = mod / "m.py"
    f.write_text("v1")

    restarts, echoes = [], []
    # simulate: poll #1 is the clean baseline, poll #2 sees the edit
    state = {"n": 0}

    real_snapshot = watcher.snapshot

    def flaky_snapshot(root, exts=("py",)):
        state["n"] += 1
        if state["n"] == 2:
            f.write_text("v2")   # edit between baseline and first poll
        return real_snapshot(root, exts)

    monkeypatch.setattr(watcher, "snapshot", flaky_snapshot)

    watcher.watch(tmp_path, exts={"py"}, interval=0, debounce=0,
                  echo=echoes.append, restart=lambda: restarts.append(1),
                  max_cycles=1)

    assert len(restarts) == 1
    assert any("change detected" in e and "m.py" in e for e in echoes)


def test_dev_requires_running_web(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: False)
    result = CliRunner().invoke(cli.main, ["dev", "acme"])
    assert result.exit_code != 0
    assert "not running" in result.output


def test_dev_requires_custom_addons_dir(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    Path(entry["custom_addons"]).rmdir()
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: True)
    result = CliRunner().invoke(cli.main, ["dev", "acme"])
    assert result.exit_code != 0
    assert "custom_addons" in result.output
