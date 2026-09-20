from click.testing import CliRunner

from odooctl import update
from odooctl.cli import main


def _enable_check(monkeypatch):
    monkeypatch.delenv("ODOOCTL_NO_UPDATE_CHECK", raising=False)


def _fake_fetch(monkeypatch, version):
    calls = []

    def fetch():
        calls.append(1)
        return version

    monkeypatch.setattr(update, "_fetch_latest_version", fetch)
    return calls


def test_parse_remote_version_variants():
    text = '"""doc."""\n\n__version__ = "1.2.3"\n'
    assert update._parse_remote_version(text) == "1.2.3"
    assert update._parse_remote_version("__version__ = '0.9.0'") == "0.9.0"
    assert update._parse_remote_version("no version here") is None


def test_version_tuple_compares_numerically():
    assert update._version_tuple("0.10.0") > update._version_tuple("0.9.0")
    assert update._version_tuple("1.0") < update._version_tuple("1.0.1")
    assert update._version_tuple("0.4.0") == update._version_tuple("0.4.0")


def test_check_for_update_reports_newer_remote(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    calls = _fake_fetch(monkeypatch, "99.0.0")
    assert update.check_for_update() == "99.0.0"
    assert len(calls) == 1


def test_check_for_update_silent_when_current(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    _fake_fetch(monkeypatch, update.__version__)
    assert update.check_for_update() is None


def test_check_for_update_uses_cache_within_interval(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    calls = _fake_fetch(monkeypatch, "99.0.0")
    assert update.check_for_update() == "99.0.0"
    assert update.check_for_update() == "99.0.0"  # cached, no second fetch
    assert len(calls) == 1


def test_check_for_update_offline_stays_quiet(isolated_config, monkeypatch):
    _enable_check(monkeypatch)

    def fetch():
        raise OSError("no route to host")

    monkeypatch.setattr(update, "_fetch_latest_version", fetch)
    assert update.check_for_update() is None
    assert not (isolated_config / "update-check.json").exists()  # failure not cached


def test_check_for_update_env_opt_out(isolated_config, monkeypatch):
    monkeypatch.setenv("ODOOCTL_NO_UPDATE_CHECK", "1")
    calls = _fake_fetch(monkeypatch, "99.0.0")
    assert update.check_for_update() is None
    assert not calls


def test_fetch_parses_remote_file_over_urllib(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    # a data: URL exercises the real urlopen path without network access
    monkeypatch.setattr(update, "REMOTE_VERSION_URL", "data:,__version__%20%3D%20%225.0.0%22")
    assert update._fetch_latest_version() == "5.0.0"


def test_update_notice_names_both_versions_and_the_clone(monkeypatch):
    monkeypatch.setattr(update, "_clone_dir", lambda: "/Users/dev/tools/odooctl")
    notice = update.update_notice("99.0.0")
    assert "odooctl 99.0.0 is available" in notice
    assert update.__version__ in notice
    assert "cd /Users/dev/tools/odooctl && git pull" in notice


def test_update_notice_without_clone_points_to_repo(monkeypatch):
    monkeypatch.setattr(update, "_clone_dir", lambda: None)
    notice = update.update_notice("99.0.0")
    assert update.REPO_URL in notice


def _register_project(tmp_path):
    from odooctl import registry

    d = tmp_path / "proj"
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}")
    registry.register(
        "proj",
        {
            "compose_file": str(d / "docker-compose.yml"),
            "path": str(d),
            "services": {"web": "web", "db": "db"},
            "container_names": {"web": "proj_web", "db": "proj_db"},
            "ports": {"http": 8069},
            "db_user": "odoo",
        },
    )


def test_cli_prints_notice_before_running(isolated_config, tmp_path, monkeypatch):
    _enable_check(monkeypatch)
    _register_project(tmp_path)
    _fake_fetch(monkeypatch, "99.0.0")
    result = CliRunner().invoke(main, ["projects"])
    assert result.exit_code == 0, result.output
    assert "odooctl 99.0.0 is available" in result.stderr


def test_cli_no_notice_when_current(isolated_config, tmp_path, monkeypatch):
    _enable_check(monkeypatch)
    _register_project(tmp_path)
    _fake_fetch(monkeypatch, update.__version__)
    result = CliRunner().invoke(main, ["projects"])
    assert result.exit_code == 0, result.output
    assert "is available" not in result.stderr
