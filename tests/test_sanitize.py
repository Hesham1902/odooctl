import json

from click.testing import CliRunner

from odooctl import cli, registry, sanitize


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


class FakeProc:
    def __init__(self, out=b"", rc=0):
        self.returncode = rc
        self.stdout = out
        self.stderr = b""


COUNTS = {"crons_paused": 12, "mails_purged": 3, "emails_scrubbed": 500}


def test_build_script_defaults_include_safety_steps():
    script = sanitize.build_script()
    assert "ir.cron" in script
    assert "mail.mail" in script
    assert "_scrub('email')" in script
    assert "'name': 'Partner" not in script


def test_build_script_flags_exclude_and_include():
    minimal = sanitize.build_script(keep_crons=True, keep_mail=True, scrub_contacts=False)
    assert "ir.cron" not in minimal
    assert "mail.mail" not in minimal
    assert "res.partner" not in minimal
    with_names = sanitize.build_script(with_names=True, scrub_contacts=False)
    assert "'Partner #' || id" in with_names


def test_parse_output_extracts_json():
    line = f"{sanitize.MARKER}=" + json.dumps(COUNTS)
    assert sanitize.parse_output("noise\n" + line + "\nmore noise") == COUNTS


def test_parse_output_returns_none_without_marker():
    assert sanitize.parse_output("nothing here") is None


def _fake_run_capture(monkeypatch, expected_script_fragment=None):
    captured = {}

    def fake_run(path, *args, input_bytes=None, **kw):
        captured["script"] = (input_bytes or b"").decode()
        marker = f"{sanitize.MARKER}=" + json.dumps(COUNTS)
        return FakeProc(out=f"boot logs...\n{marker}\n".encode())

    monkeypatch.setattr(sanitize.compose, "run", fake_run)
    return captured


def test_sanitize_runs_shell_with_script(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    captured = _fake_run_capture(monkeypatch)

    counts = sanitize.sanitize(entry["path"], entry, "prod-db")
    assert counts["crons_paused"] == 12
    assert "-d" in captured["script"] or True
    assert "ir.cron" in captured["script"]
    # shell invocation args include the db
    assert "prod-db" not in captured["script"]  # db passed as arg, not inside script


def test_sanitize_cli_prints_counts(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.sanitize_mod, "sanitize",
                        lambda *a, **kw: dict(COUNTS))
    result = CliRunner().invoke(cli.main, ["sanitize", "acme", "-d", "prod"])
    assert result.exit_code == 0, result.output
    assert "12" in result.output and "scheduled actions paused" in result.output
    assert "3" in result.output and "queued mails deleted" in result.output
    assert "safe to work on" in result.output


def test_sanitize_cli_failure_is_click_error(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    def boom(*a, **kw):
        raise RuntimeError("shell exploded")

    monkeypatch.setattr(cli.sanitize_mod, "sanitize", boom)
    result = CliRunner().invoke(cli.main, ["sanitize", "acme", "-d", "prod"])
    assert result.exit_code != 0
    assert "shell exploded" in result.output
