from pathlib import Path

import pytest
from click.testing import CliRunner

from odooctl import cli, pull, registry


def _register(slug, tmp_path):
    d = tmp_path / slug
    d.mkdir()
    (d / "docker-compose.yml").write_text("services: {}")
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


class FakeProc:
    def __init__(self, out=b"", rc=0, err=b""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_parse_target_variants():
    assert pull.parse_target("user@host") == ("user@host", None)
    assert pull.parse_target("ssh://user@host") == ("user@host", None)
    assert pull.parse_target("ssh://user@host:2222") == ("user@host", 2222)


def test_parse_target_rejects_garbage():
    with pytest.raises(pull.PullError):
        pull.parse_target("just-a-host")
    with pytest.raises(pull.PullError):
        pull.parse_target("")


FOUND_OUTPUT = (
    b"ODOOCTL_OK\n"
    b"SQL_GZ=/backup.daily/db_20260821.sql.gz\n"
    b"MIRROR=/backup.daily/db_20260821\n"
)


def test_find_remote_backup_uses_a_single_ssh_command(monkeypatch):
    calls = []

    def fake_run(cmd, capture_output=True):
        calls.append(cmd[-1])
        return FakeProc(out=FOUND_OUTPUT)

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    found = pull.find_remote_backup("u@h")
    assert found == {
        "sql_gz": "/backup.daily/db_20260821.sql.gz",
        "mirror": "/backup.daily/db_20260821",
    }
    # probe + dir scan + mirror check all folded into one exec
    assert len(calls) == 1
    assert "ODOOCTL_OK" in calls[0] and "*.sql.gz" in calls[0] and "MIRROR" in calls[0]


def test_find_remote_backup_mirror_missing(monkeypatch):
    def fake_run(cmd, capture_output=True):
        return FakeProc(out=b"ODOOCTL_OK\nSQL_GZ=/backup.daily/db_20260821.sql.gz\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    found = pull.find_remote_backup("u@h")
    assert found == {"sql_gz": "/backup.daily/db_20260821.sql.gz", "mirror": None}


def test_ssh_failure_surfaces_ssh_error(monkeypatch):
    def fake_run(cmd, capture_output=True):
        return FakeProc(rc=255, err=b"Permission denied (publickey)")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    with pytest.raises(pull.PullError) as exc:
        pull.find_remote_backup("u@h")
    assert "Permission denied" in str(exc.value)
    assert "'SSH' button" in str(exc.value)


def test_find_remote_backup_persistent_rejection_is_an_ssh_error(monkeypatch):
    """The odoo.sh gateway flake must not masquerade as 'No backup found'."""

    def fake_run(cmd, capture_output=True):
        return FakeProc(rc=255, err=b"exec request failed on channel 0")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    monkeypatch.setattr(pull.time, "sleep", lambda s: None)
    with pytest.raises(pull.PullError) as exc:
        pull.find_remote_backup("u@h")
    assert "Cannot reach" in str(exc.value)
    assert "No backup" not in str(exc.value)


def test_find_remote_backup_retries_transient_exec_rejections(monkeypatch):
    attempts = []

    def fake_run(cmd, capture_output=True):
        attempts.append(1)
        if len(attempts) < 3:
            return FakeProc(rc=255, err=b"exec request failed on channel 0")
        return FakeProc(out=FOUND_OUTPUT)

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    sleeps = []
    monkeypatch.setattr(pull.time, "sleep", lambda s: sleeps.append(s))
    found = pull.find_remote_backup("u@h")
    assert found["sql_gz"] == "/backup.daily/db_20260821.sql.gz"
    assert len(attempts) == 3
    assert len(sleeps) == 2


def test_find_remote_backup_no_backup_lists_dirs(monkeypatch):
    monkeypatch.setattr(pull.subprocess, "run", lambda cmd, **kw: FakeProc(out=b"ODOOCTL_OK\n"))
    with pytest.raises(pull.PullError) as exc:
        pull.find_remote_backup("u@h")
    assert "--path" in str(exc.value)
    assert "backup.daily" in str(exc.value)


def test_find_remote_backup_explicit_dir_path(monkeypatch):
    seen = {}

    def fake_run(cmd, capture_output=True):
        seen["cmd"] = cmd[-1]
        return FakeProc(out=b"ODOOCTL_OK\nSQL_GZ=/tmp/x.sql.gz\nMIRROR=/tmp/x\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    found = pull.find_remote_backup("u@h", path="/tmp")
    assert found == {"sql_gz": "/tmp/x.sql.gz", "mirror": "/tmp/x"}
    assert "/tmp" in seen["cmd"]


def test_find_remote_backup_accepts_direct_file_path(monkeypatch):
    seen = {}

    def fake_run(cmd, capture_output=True):
        seen["cmd"] = cmd[-1]
        return FakeProc(out=b"ODOOCTL_OK\nSQL_GZ=/b/acme.sql.gz\nMIRROR=/b/acme\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    found = pull.find_remote_backup("u@h", path="/b/acme.sql.gz")
    assert found == {"sql_gz": "/b/acme.sql.gz", "mirror": "/b/acme"}
    assert "[ -f " in seen["cmd"] and "/b/acme.sql.gz" in seen["cmd"]
    assert "for d in" not in seen["cmd"]


def test_probe_retries_transient_rejections(monkeypatch):
    attempts = []

    def fake_run(cmd, capture_output=True):
        attempts.append(cmd[-1])
        if len(attempts) < 2:
            return FakeProc(rc=255, err=b"exec request failed on channel 0")
        return FakeProc(out=b"ODOOCTL_OK\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    monkeypatch.setattr(pull.time, "sleep", lambda s: None)
    pull.probe("u@h")
    assert len(attempts) == 2


def test_scan_script_leaves_safe_words_bare_and_quotes_unsafe_ones():
    script = pull._scan_script(["~/backup.daily", "/backup.daily", "/my backups"])
    # safe words stay unquoted so the remote shell expands ~ and globs itself
    assert script.index("~/backup.daily") < script.index("/backup.daily")
    assert "'/my backups'" in script
    assert "case " not in script  # odoo.sh's shell expands ~ in ${d#~} patterns
    assert "MIRROR=" in script


def test_download_builds_bundle(tmp_path, monkeypatch):
    seen = []

    def fake_run(cmd, capture_output=True):
        seen.append(cmd)
        if cmd[0] == "scp":
            Path(cmd[-1]).write_bytes(b"sqldata")
            return FakeProc()
        return FakeProc(out=b"no\n")  # no remote data dir -> skip streaming

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    bundle = pull.download(
        "u@h", 2222, {"sql_gz": "/backup.daily/acme_20260821.sql.gz", "mirror": None}, tmp_path
    )
    assert bundle.name == "acme_20260821"
    assert (bundle / "acme_20260821.sql.gz").read_bytes() == b"sqldata"
    scp_cmd = next(c for c in seen if c[0] == "scp")
    assert "-P" in scp_cmd and "2222" in scp_cmd


def test_download_streams_filestore_when_present(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            p = Path(cmd[-1])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            return FakeProc()
        if "[ -d " in cmd[-1]:
            return FakeProc(out=b"yes\n")
        return FakeProc(out=b"")

    streamed = {}

    def fake_stream(target, port, remote_dir, remote_sub, dest, key=None):
        streamed["dir"], streamed["sub"] = remote_dir, remote_sub
        fs = Path(dest) / "home" / "odoo" / "data" / "filestore" / "db1"
        fs.mkdir(parents=True)
        (fs / "blob").write_text("f")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    monkeypatch.setattr(pull, "_stream_remote_tar", fake_stream)

    bundle = pull.download(
        "u@h", None, {"sql_gz": "/b/acme.sql.gz", "mirror": "/b/acme"}, tmp_path, with_filestore=True
    )
    assert streamed == {"dir": "/b/acme", "sub": "home/odoo/data"}
    assert (bundle / "home" / "odoo" / "data" / "filestore" / "db1" / "blob").exists()


def test_download_skips_filestore_by_default(tmp_path, monkeypatch):
    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            p = Path(cmd[-1])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            return FakeProc()
        if "[ -d " in cmd[-1]:
            return FakeProc(out=b"yes\n")
        return FakeProc(out=b"")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    monkeypatch.setattr(
        pull, "_stream_remote_tar", lambda *a, **kw: pytest.fail("must not stream without --with-filestore")
    )

    # leftovers from an aborted --with-filestore run must be cleaned
    stale = tmp_path / "acme" / "home" / "odoo" / "data" / "filestore"
    stale.mkdir(parents=True)

    bundle = pull.download("u@h", None, {"sql_gz": "/b/acme.sql.gz", "mirror": "/b/acme"}, tmp_path)
    assert (bundle / "acme.sql.gz").exists()
    assert not (bundle / "home").exists()


def test_download_reuses_cached_sql_gz(tmp_path, monkeypatch):
    import gzip

    scp_calls = []

    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            scp_calls.append(cmd)
            return FakeProc(rc=1, err=b"should not be called")
        return FakeProc(out=b"no\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    bundle = tmp_path / "acme"
    bundle.mkdir()
    with gzip.open(bundle / "acme.sql.gz", "wt") as fh:
        fh.write("already here")

    out = pull.download("u@h", None, {"sql_gz": "/b/acme.sql.gz", "mirror": None}, tmp_path)
    assert not scp_calls
    with gzip.open(out / "acme.sql.gz", "rt") as fh:
        assert fh.read() == "already here"


def test_download_reuses_cache_via_remote_size_match_without_decompressing(tmp_path, monkeypatch):
    scp_calls = []

    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            scp_calls.append(cmd)
            return FakeProc(rc=1, err=b"should not be called")
        if "stat -c%s" in cmd[-1]:
            return FakeProc(out=b"11\n")
        return FakeProc(out=b"no\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    bundle = tmp_path / "acme"
    bundle.mkdir()
    # not valid gzip at all - proves the size-match path skips the expensive
    # decompress check rather than falling back to it
    (bundle / "acme.sql.gz").write_bytes(b"not-gzip-11")

    out = pull.download("u@h", None, {"sql_gz": "/b/acme.sql.gz", "mirror": None}, tmp_path)
    assert not scp_calls
    assert (out / "acme.sql.gz").read_bytes() == b"not-gzip-11"


def test_download_redownloads_when_remote_size_mismatches_cache(tmp_path, monkeypatch):
    scp_calls = []

    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            scp_calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"fresh good data")
            return FakeProc()
        if "stat -c%s" in cmd[-1]:
            return FakeProc(out=b"999\n")  # remote is a different size than the cache
        return FakeProc(out=b"no\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    bundle = tmp_path / "acme"
    bundle.mkdir()
    (bundle / "acme.sql.gz").write_bytes(b"stale cached data")

    out = pull.download("u@h", None, {"sql_gz": "/b/acme.sql.gz", "mirror": None}, tmp_path)
    assert len(scp_calls) == 1
    assert (out / "acme.sql.gz").read_bytes() == b"fresh good data"


def test_download_redownloads_when_cache_is_truncated(tmp_path, monkeypatch):
    import gzip

    scp_calls = []

    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            scp_calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"fresh good data")
            return FakeProc()
        return FakeProc(out=b"no\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    bundle = tmp_path / "acme"
    bundle.mkdir()
    cached = bundle / "acme.sql.gz"
    with gzip.open(cached, "wt") as fh:
        fh.write("a fully compressed line that will get cut off before the end")
    # simulate an interrupted scp: truncate past the gzip header, before the
    # end-of-stream marker/CRC trailer
    cached.write_bytes(cached.read_bytes()[:10])

    out = pull.download("u@h", None, {"sql_gz": "/b/acme.sql.gz", "mirror": None}, tmp_path)
    assert len(scp_calls) == 1
    assert (out / "acme.sql.gz").read_bytes() == b"fresh good data"


def test_download_failure_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pull.subprocess, "run", lambda cmd, **kw: FakeProc(rc=1, err=b"permission denied"))
    with pytest.raises(pull.PullError) as exc:
        pull.download("u@h", None, {"sql_gz": "/x.sql.gz", "mirror": None}, tmp_path)
    assert "permission denied" in str(exc.value)


def test_download_retries_legacy_scp_on_sftp_error(tmp_path, monkeypatch):
    attempts = []

    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            attempts.append(cmd)
            if "-O" not in cmd:
                return FakeProc(rc=1, err=b"subsystem request failed on channel 0")
            Path(cmd[-1]).write_bytes(b"data")
            return FakeProc()
        return FakeProc(out=b"no\n")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    bundle = pull.download("u@h", None, {"sql_gz": "/b/a.sql.gz", "mirror": None}, tmp_path)
    assert len(attempts) == 2
    assert "-O" in attempts[1]
    assert (bundle / "a.sql.gz").read_bytes() == b"data"


def test_download_no_retry_for_other_errors(tmp_path, monkeypatch):
    attempts = []

    def fake_run(cmd, capture_output=True):
        if cmd[0] == "scp":
            attempts.append(cmd)
        return FakeProc(rc=1, err=b"connection refused")

    monkeypatch.setattr(pull.subprocess, "run", fake_run)
    with pytest.raises(pull.PullError):
        pull.download("u@h", None, {"sql_gz": "/x.sql.gz", "mirror": None}, tmp_path)
    assert len(attempts) == 1


@pytest.fixture
def happy_pull(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": [])
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: ("running", {}))
    monkeypatch.setattr(
        cli.pull_mod,
        "find_remote_backup",
        lambda t, port=None, path=None, key=None: {
            "sql_gz": "~/backup.daily/acme_20260821.sql.gz",
            "mirror": "~/backup.daily/acme_20260821",
        },
    )

    zips = {}
    lifecycle = []

    def fake_download(t, port, remote, dest_dir, key=None, with_filestore=False):
        bundle = Path(dest_dir) / "acme_20260821"
        bundle.mkdir(parents=True)
        (bundle / "acme_20260821.sql.gz").write_bytes(b"\x1f\x8b sql")
        zips["local"] = bundle
        return bundle

    monkeypatch.setattr(cli.pull_mod, "download", fake_download)
    monkeypatch.setattr(cli.restore_mod, "detect_format", lambda p: "odoosh_raw")
    monkeypatch.setattr(cli.restore_mod, "target_name", lambda p, fmt, name: name or "acme_prod")
    monkeypatch.setattr(cli.restore_mod, "_dump_create_target", lambda gz: None)

    def fake_restore(path, ent, src, db):
        lifecycle.append(("restore", db))
        return {"filestore": True}

    def fake_reset(path, ent, db):
        lifecycle.append("reset_admin")
        return {"id": 2, "old_login": "x"}

    monkeypatch.setattr(cli.restore_mod, "restore", fake_restore)
    monkeypatch.setattr(cli.admin, "reset_admin", fake_reset)
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: True)
    monkeypatch.setattr(
        cli.compose,
        "run",
        lambda path, *a, **kw: lifecycle.append(a) or FakeProc(out=b"ODOOCTL_SANITIZE_OK={}\n"),
    )
    return entry, zips, lifecycle


def test_pull_full_flow_downloads_restores_cleans(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    result = CliRunner().invoke(
        cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh", "-d", "acme_prod"]
    )
    assert result.exit_code == 0, result.output
    # web stopped before restore, sanitized, and restarted
    assert lifecycle[0] == ("stop", "web")
    assert lifecycle[1] == ("restore", "acme_prod")
    assert lifecycle[2] == "reset_admin"
    assert lifecycle[3][0] == "run"
    assert lifecycle[4] == ("start", "web")
    assert not zips["local"].exists()  # cleaned up by default
    assert "cleaned up download" in result.output


def test_pull_keeps_web_down_when_it_was_down(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: False)
    result = CliRunner().invoke(cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh"])
    assert result.exit_code == 0, result.output
    assert ("stop", "web") not in lifecycle
    assert ("start", "web") not in lifecycle


def test_pull_requires_running_db_container(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: ("stopped", {}))
    result = CliRunner().invoke(cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh"])
    assert result.exit_code != 0
    assert "db container is not running" in result.output


def test_pull_default_db_name_when_none_given(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    result = CliRunner().invoke(cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh"])
    assert result.exit_code == 0, result.output
    assert "restoring as 'acme_pulled'" in result.output
    assert lifecycle[1] == ("restore", "acme_pulled")


def test_pull_confirm_before_download_and_abort(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": ["acme_pulled"])
    downloads = []

    def fake_download(*a, **kw):
        downloads.append(1)
        raise AssertionError("must not download when aborted")

    monkeypatch.setattr(cli.pull_mod, "download", fake_download)
    result = CliRunner().invoke(cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh"], input="n\n")
    assert result.exit_code != 0
    assert not downloads


def test_pull_yes_flag_skips_confirm(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": ["acme_prod"])
    result = CliRunner().invoke(
        cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh", "-d", "acme_prod", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert lifecycle[1] == ("restore", "acme_prod")


def test_pull_reports_source_db_from_dump(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    monkeypatch.setattr(cli.restore_mod, "_dump_create_target", lambda gz: "08-10-2026")
    result = CliRunner().invoke(
        cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh", "-d", "acme_prod"]
    )
    assert result.exit_code == 0, result.output
    assert "source database in dump: '08-10-2026' -> will become 'acme_prod'" in result.output


def test_pull_without_filestore_repairs_icons(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull

    def fake_restore(path, ent, src, db):
        lifecycle.append(("restore", db))
        return {"filestore": False}

    monkeypatch.setattr(cli.restore_mod, "restore", fake_restore)
    icon_calls = []
    monkeypatch.setattr(
        cli.icons_mod,
        "fix_icons",
        lambda path, ent, db: icon_calls.append(db) or {"checked": 45, "fixed": 4, "unrepairable": 0},
    )

    result = CliRunner().invoke(
        cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh", "-d", "acme_prod"]
    )
    assert result.exit_code == 0, result.output
    assert icon_calls == ["acme_prod"]
    assert "re-importing menu icons" in result.output
    assert "re-imported 4" in result.output


def test_pull_with_filestore_skips_icon_repair(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    icon_calls = []
    monkeypatch.setattr(cli.icons_mod, "fix_icons", lambda *a: icon_calls.append(1))
    result = CliRunner().invoke(
        cli.main, ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh", "-d", "acme_prod"]
    )
    assert result.exit_code == 0, result.output
    assert not icon_calls


def test_pull_no_fix_icons_skips_icon_repair(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    monkeypatch.setattr(cli.restore_mod, "restore", lambda *a: {"filestore": False})
    icon_calls = []
    monkeypatch.setattr(cli.icons_mod, "fix_icons", lambda *a: icon_calls.append(1))
    result = CliRunner().invoke(
        cli.main,
        ["pull", "acme", "--from", "ssh://acme@acme.odoo.sh", "--no-fix-icons"],
    )
    assert result.exit_code == 0, result.output
    assert not icon_calls
    assert "icon repair skipped" in result.output


def test_pull_keep_download_flag(happy_pull, tmp_path, monkeypatch):
    entry, zips, lifecycle = happy_pull
    result = CliRunner().invoke(cli.main, ["pull", "acme", "--from", "acme@acme.odoo.sh", "--keep-download"])
    assert result.exit_code == 0, result.output
    assert zips["local"].exists()
    assert "bundle kept at" in result.output


def test_pull_without_from_and_no_saved_settings_errors(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    result = CliRunner().invoke(cli.main, ["pull", "acme"])
    assert result.exit_code != 0
    assert "--save" in result.output


def test_pull_save_then_plain_reuse(tmp_path, monkeypatch):
    from odooctl import registry

    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "databases", lambda p, u="odoo": [])
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: ("running", {}))
    monkeypatch.setattr(cli.compose, "web_running", lambda p, e: False)
    monkeypatch.setattr(cli.compose, "run", lambda path, *a, **kw: FakeProc())

    seen = {}

    def fake_find(target, port=None, path=None, key=None):
        seen.update(target=target, key=str(key) if key else None)
        return {"sql_gz": "~/backup.daily/acme_20260821.sql.gz", "mirror": None}

    def fake_download(t, port, remote, dest_dir, key=None, with_filestore=False):
        bundle = Path(dest_dir) / "acme_20260821"
        bundle.mkdir(parents=True)
        (bundle / "acme_20260821.sql.gz").write_bytes(b"\x1f\x8b")
        return bundle

    monkeypatch.setattr(cli.pull_mod, "find_remote_backup", fake_find)
    monkeypatch.setattr(cli.pull_mod, "download", fake_download)
    monkeypatch.setattr(cli.restore_mod, "detect_format", lambda p: "odoosh_raw")
    monkeypatch.setattr(cli.restore_mod, "target_name", lambda p, fmt, name: name or "acme_prod")
    monkeypatch.setattr(cli.restore_mod, "restore", lambda path, ent, src, db: {"filestore": False})
    monkeypatch.setattr(cli.admin, "reset_admin", lambda path, ent, db: {"id": 2, "old_login": "x"})
    # --key needs an existing file
    key_file = tmp_path / "k_ed25519"
    key_file.write_text("PRIVATE KEY")

    # 1st run: full flags + --save
    r1 = CliRunner().invoke(
        cli.main,
        [
            "pull",
            "acme",
            "--from",
            "ssh://1234567@acme.odoo.com",
            "--key",
            str(key_file),
            "-d",
            "acme_prod",
            "--save",
        ],
    )
    assert r1.exit_code == 0, r1.output
    assert "saved pull settings" in r1.output
    saved = registry.load_pull_settings("acme")
    assert saved["from"] == "ssh://1234567@acme.odoo.com"
    assert saved["db"] == "acme_prod"
    assert saved["key"] == str(key_file)

    # 2nd run: project only
    r2 = CliRunner().invoke(cli.main, ["pull", "acme"])
    assert r2.exit_code == 0, r2.output
    assert seen["target"] == "1234567@acme.odoo.com"
    assert seen["key"] == str(key_file)
