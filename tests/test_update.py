import json
import time
from urllib.parse import quote

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


def test_version_tuple_compares_numerically():
    assert update._version_tuple("0.10.0") > update._version_tuple("0.9.0")
    assert update._version_tuple("1.0") < update._version_tuple("1.0.1")
    assert update._version_tuple("0.4.0") == update._version_tuple("0.4.0")
    assert update._version_tuple("0.7.0rc1") is None


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
    assert json.loads((isolated_config / "update-check.json").read_text())["failed"] is True
    assert update.check_for_update() is None


def test_failed_check_retries_after_shorter_interval(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("offline")
        return "99.0.0"

    monkeypatch.setattr(update, "_fetch_latest_version", fetch)
    assert update.check_for_update() is None
    assert update.check_for_update() is None
    assert len(calls) == 1
    cache = json.loads((isolated_config / "update-check.json").read_text())
    cache["checked_at"] = time.time() - update.RETRY_INTERVAL - 1
    (isolated_config / "update-check.json").write_text(json.dumps(cache))
    assert update.check_for_update() == "99.0.0"
    assert len(calls) == 2


def test_check_for_update_env_opt_out(isolated_config, monkeypatch):
    monkeypatch.setenv("ODOOCTL_NO_UPDATE_CHECK", "1")
    calls = _fake_fetch(monkeypatch, "99.0.0")
    assert update.check_for_update() is None
    assert not calls


def test_zero_does_not_disable_check(isolated_config, monkeypatch):
    monkeypatch.setenv("ODOOCTL_NO_UPDATE_CHECK", "0")
    calls = _fake_fetch(monkeypatch, "99.0.0")
    assert update.check_for_update() == "99.0.0"
    assert len(calls) == 1


def test_bad_cache_never_breaks_check(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    cache_path = isolated_config / "update-check.json"
    isolated_config.mkdir(exist_ok=True)
    calls = _fake_fetch(monkeypatch, "99.0.0")
    for checked_at, latest in [
        ("not a number", "99.0.0"),
        (10**1000, "99.0.0"),
        (float("nan"), "99.0.0"),
        (time.time() + 86400, "99.0.0"),
        (time.time(), {"unexpected": "value"}),
    ]:
        cache_path.write_text(
            json.dumps(
                {"source": update._CACHE_SOURCE, "checked_at": checked_at, "latest": latest}
            )
        )
        assert update.check_for_update() == "99.0.0"
    assert len(calls) == 5


def test_fetch_parses_published_release_over_urllib(isolated_config, monkeypatch):
    _enable_check(monkeypatch)
    # a data: URL exercises the real urlopen path without network access
    release = {"tag_name": "v5.0.0", "draft": False, "prerelease": False}
    monkeypatch.setattr(update, "RELEASE_API_URL", f"data:,{quote(json.dumps(release))}")
    assert update._fetch_latest_version() == "5.0.0"


def test_fetch_rejects_invalid_or_unpublished_release(monkeypatch):
    for release in (
        {"tag_name": "v5.0.0", "draft": True},
        {"tag_name": "v5.0.0", "prerelease": True},
        {"tag_name": "v5.0.0rc1"},
        {"tag_name": "other"},
    ):
        monkeypatch.setattr(update, "RELEASE_API_URL", f"data:,{quote(json.dumps(release))}")
        try:
            update._fetch_latest_version()
        except ValueError:
            pass
        else:
            raise AssertionError(f"Accepted invalid release: {release}")


def test_update_notice_names_both_versions_and_the_clone(monkeypatch):
    monkeypatch.setattr(update, "_clone_dir", lambda: "/Users/dev/tools/odooctl")
    notice = update.update_notice("99.0.0")
    assert "odooctl 99.0.0 is available" in notice
    assert update.__version__ in notice
    assert "/Users/dev/tools/odooctl" in notice
    assert update.release_url("99.0.0") in notice


def test_update_notice_without_clone_points_to_repo(monkeypatch):
    monkeypatch.setattr(update, "_clone_dir", lambda: None)
    notice = update.update_notice("99.0.0")
    assert update.release_url("99.0.0") in notice


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
