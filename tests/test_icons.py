from click.testing import CliRunner

from odooctl import cli, icons, registry


def _register(slug, tmp_path):
    d = tmp_path / slug
    d.mkdir()
    registry.register(
        slug,
        {
            "compose_file": str(d / "docker-compose.yml"),
            "path": str(d),
            "services": {"web": "web", "db": "db"},
            "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
            "ports": {"http": 8069},
            "db_user": "odoo",
        },
    )
    return registry.get_projects()[slug]


def test_script_repairs_menu_icon_attachments():
    assert "ir.ui.menu" in icons.SCRIPT
    assert "_filestore" in icons.SCRIPT
    assert "'raw': data" in icons.SCRIPT
    assert "/mnt/extra-addons" in icons.SCRIPT
    assert "ODOOCTL_ICONS_OK=" in icons.SCRIPT
    assert "{{" not in icons.SCRIPT  # brace-escaping bug would ship literal {{


def test_parse_output_extracts_counts():
    line = f"{icons.MARKER}=" + '{"checked": 45, "fixed": 4, "unrepairable": 1}'
    assert icons.parse_output("boot...\n" + line) == {
        "checked": 45,
        "fixed": 4,
        "unrepairable": 1,
    }


def test_parse_output_none_without_marker():
    assert icons.parse_output("no marker here") is None


class FakeProc:
    def __init__(self, out=b"", rc=0):
        self.returncode = rc
        self.stdout = out
        self.stderr = b""


def test_fix_icons_runs_shell_with_script(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    captured = {}

    def fake_run(path, *args, input_bytes=None, **kw):
        captured["script"] = (input_bytes or b"").decode()
        marker = f"{icons.MARKER}=" + '{"checked": 45, "fixed": 4, "unrepairable": 0}'
        return FakeProc(out=f"log\n{marker}\n".encode())

    monkeypatch.setattr(icons.compose, "run", fake_run)
    counts = icons.fix_icons(entry["path"], entry, "acme-prod")
    assert counts == {"checked": 45, "fixed": 4, "unrepairable": 0}
    assert "ir.ui.menu" in captured["script"]


def test_fix_icons_failure_raises(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    monkeypatch.setattr(icons.compose, "run", lambda *a, **kw: FakeProc(out=b"traceback boom", rc=1))
    try:
        icons.fix_icons(entry["path"], entry, "acme-prod")
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:
        raise AssertionError("should have raised")


def test_cli_fix_icons_prints_summary(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": ["acme-prod"])
    monkeypatch.setattr(cli.icons_mod, "fix_icons", lambda *a: {"checked": 45, "fixed": 4, "unrepairable": 1})
    result = CliRunner().invoke(cli.main, ["fix-icons", "acme"])
    assert result.exit_code == 0, result.output
    assert "checked 45 menu icon(s), re-imported 4" in result.output
    assert "1 without a source file" in result.output
    assert "restart web" in result.output


def test_cli_fix_icons_db_flag(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    seen = {}
    monkeypatch.setattr(
        cli.icons_mod,
        "fix_icons",
        lambda path, ent, db: seen.update(db=db) or {"checked": 1, "fixed": 0, "unrepairable": 0},
    )
    result = CliRunner().invoke(cli.main, ["fix-icons", "acme", "-d", "other"])
    assert result.exit_code == 0, result.output
    assert seen == {"db": "other"}
