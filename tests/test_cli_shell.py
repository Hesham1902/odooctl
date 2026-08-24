from click.testing import CliRunner

from odooctl import cli, registry


def _register(slug, tmp_path):
    d = tmp_path / slug
    d.mkdir()
    registry.register(slug, {
        "compose_file": str(d / "docker-compose.yml"),
        "path": str(d),
        "services": {"web": "web", "db": "db"},
        "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
        "ports": {"http": 8069},
        "db_user": "odoo",
    })
    return registry.get_projects()[slug]


def test_shell_requires_running_web(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: False)

    result = CliRunner().invoke(cli.main, ["shell", "acme"])
    assert result.exit_code != 0
    assert "not running" in result.output


def test_shell_auto_picks_single_db(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: True)
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": ["acme-prod"])

    calls = []

    class FakeProc:
        returncode = 0
        stdout = b""

    monkeypatch.setattr(cli.compose, "run",
                        lambda path, *a, **kw: (calls.append((path, a)), FakeProc())[1])

    result = CliRunner().invoke(cli.main, ["shell", "acme"])
    assert result.exit_code == 0, result.output
    path, args = calls[0]
    assert str(path) == entry["path"]
    assert args[:2] == ("exec", "web")
    assert "odoo" in args and "shell" in args
    assert "-d" in args and "acme-prod" in args
    assert "--no-http" in args


def test_shell_multiple_dbs_needs_flag(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: True)
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": ["acme-a", "acme-b"])

    result = CliRunner().invoke(cli.main, ["shell", "acme"])
    assert result.exit_code != 0
    assert "Multiple databases" in result.output
    assert "--db" in result.output


def test_shell_explicit_db_skips_lookup(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: True)
    lookups = []
    monkeypatch.setattr(cli.compose, "databases",
                        lambda p, u="odoo": lookups.append(p) or [])

    calls = []

    class FakeProc:
        returncode = 0
        stdout = b""

    monkeypatch.setattr(cli.compose, "run",
                        lambda path, *a, **kw: (calls.append(a), FakeProc())[1])

    result = CliRunner().invoke(cli.main, ["shell", "acme", "-d", "other"])
    assert result.exit_code == 0, result.output
    assert not lookups
    args = calls[0]
    idx = list(args).index("-d")
    assert list(args)[idx + 1] == "other"
