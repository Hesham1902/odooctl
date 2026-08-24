from click.testing import CliRunner

from odooctl import cli, registry
from odooctl.dbdiff import compare, parse_module_states


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


def test_parse_module_states():
    out = "sale|installed|18.0.1.0.0\ncrm|uninstalled|-\nbroken\n"
    states = parse_module_states(out)
    assert states == {"sale": ("installed", "18.0.1.0.0"), "crm": ("uninstalled", "-")}


def test_compare_reports_changed_and_only():
    a = {"same": ("installed", "1"), "mod": ("installed", "1.0"), "gone": ("installed", "2")}
    b = {"same": ("installed", "1"), "mod": ("uninstalled", "-"), "new": ("installed", "9")}
    res = compare(a, b)
    assert res["changed"] == {"mod": (("installed", "1.0"), ("uninstalled", "-"))}
    assert res["only_a"] == ["gone"]
    assert res["only_b"] == ["new"]


def test_compare_in_sync():
    a = {"m": ("installed", "1")}
    assert compare(a, dict(a)) == {"changed": {}, "only_a": [], "only_b": []}


class FakeProc:
    def __init__(self, out):
        self.returncode = 0
        self.stdout = out.encode()


def test_diff_cli_prints_differences(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    dumps = {
        "prod": "sale|installed|18.0.1.0.0\nstock|installed|18.0.2.0.0\nold|installed|1.0\n",
        "test": "sale|uninstalled|-\nstock|installed|18.0.3.0.0\n",
    }

    def fake_exec(path, service, *cmd, **kw):
        db = cmd[cmd.index("-d") + 1]
        return FakeProc(dumps[db])

    monkeypatch.setattr(cli.compose, "exec_service", fake_exec)
    result = CliRunner().invoke(cli.main, ["diff", "acme", "prod", "test"])
    assert result.exit_code == 0, result.output
    assert "sale" in result.output and "uninstalled" in result.output
    assert "18.0.2.0.0" in result.output and "18.0.3.0.0" in result.output
    assert "only in prod (1): old" in result.output
    assert "3 module(s) differ" in result.output


def test_diff_cli_in_sync_message(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    def fake_exec(path, service, *cmd, **kw):
        return FakeProc("sale|installed|18.0.1\n")

    monkeypatch.setattr(cli.compose, "exec_service", fake_exec)
    result = CliRunner().invoke(cli.main, ["diff", "acme", "a", "b"])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.output


def test_diff_cli_grep_filters(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    def fake_exec(path, service, *cmd, **kw):
        return FakeProc("sale_a|installed|1\nsale_b|installed|2\n")

    monkeypatch.setattr(cli.compose, "exec_service", fake_exec)
    result = CliRunner().invoke(cli.main, ["diff", "acme", "x", "y", "-g", "_b"])
    assert result.exit_code == 0, result.output
    assert "sale_a" not in result.output
    assert "sale_b" not in result.output  # same on both sides -> no diff row
