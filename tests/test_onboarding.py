import zipfile
from pathlib import Path

import pytest
from conftest import write_compose

from odooctl import onboarding
from odooctl.registry import parse_compose

# ---------------------------------------------------------------------------
# InitError formatting
# ---------------------------------------------------------------------------


def test_init_error_renders_message_hint_and_nothing_changed():
    err = onboarding.InitError("boom", hint="try again")
    assert str(err) == "boom\nHint: try again\nNothing was changed."


def test_init_error_without_hint_or_nothing_changed():
    err = onboarding.InitError("boom", nothing_changed=False)
    assert str(err) == "boom"


# ---------------------------------------------------------------------------
# Addon repo spec parsing
# ---------------------------------------------------------------------------


def test_parse_addon_repo_specs_derives_name_and_ref():
    specs = onboarding.parse_addon_repo_specs(["https://github.com/OCA/queue.git#18.0"])
    assert len(specs) == 1
    assert specs[0].url == "https://github.com/OCA/queue.git"
    assert specs[0].ref == "18.0"
    assert specs[0].name == "queue"


def test_parse_addon_repo_specs_no_ref():
    specs = onboarding.parse_addon_repo_specs(["git@github.com:acme/my-addon.git"])
    assert specs[0].ref is None
    assert specs[0].name == "my-addon"


def test_parse_addon_repo_specs_dedupes_names():
    specs = onboarding.parse_addon_repo_specs(
        ["https://example.com/a/queue.git", "https://example.com/b/queue.git"]
    )
    names = [s.name for s in specs]
    assert names == ["queue", "queue-2"]


def test_parse_addon_repo_specs_rejects_empty_url():
    with pytest.raises(onboarding.InitError) as exc:
        onboarding.parse_addon_repo_specs(["#18.0"])
    assert "missing a repository URL" in str(exc.value)


def test_parse_addon_repo_specs_skips_blank_entries():
    assert onboarding.parse_addon_repo_specs(["", "   "]) == []


def test_derive_repo_name_strips_git_suffix_and_slugifies():
    assert onboarding.derive_repo_name("https://github.com/OCA/Queue-Job.git") == "queue-job"


# ---------------------------------------------------------------------------
# Backup validation
# ---------------------------------------------------------------------------


def test_validate_backup_missing_path(tmp_path):
    with pytest.raises(onboarding.InitError) as exc:
        onboarding.validate_backup(tmp_path / "nope.zip")
    assert "not found" in str(exc.value)


def test_validate_backup_unrecognized_format(tmp_path):
    bogus = tmp_path / "backup.txt"
    bogus.write_text("hi")
    with pytest.raises(onboarding.InitError) as exc:
        onboarding.validate_backup(bogus)
    assert "Unrecognized backup format" in str(exc.value)


def test_validate_backup_corrupt_zip(tmp_path):
    bad_zip = tmp_path / "backup.zip"
    bad_zip.write_bytes(b"not a real zip")
    with pytest.raises(onboarding.InitError) as exc:
        onboarding.validate_backup(bad_zip)
    assert "isn't a valid zip archive" in str(exc.value)


def test_validate_backup_valid_zip(tmp_path):
    good_zip = tmp_path / "backup.zip"
    with zipfile.ZipFile(good_zip, "w") as zf:
        zf.writestr("dump.sql", "select 1;")
    assert onboarding.validate_backup(good_zip) == "zip"


def test_validate_backup_dump_missing_header(tmp_path):
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"not a pg dump")
    with pytest.raises(onboarding.InitError) as exc:
        onboarding.validate_backup(dump)
    assert "PGDMP" in str(exc.value)


def test_validate_backup_dump_valid_header(tmp_path):
    dump = tmp_path / "backup.dump"
    dump.write_bytes(b"PGDMP" + b"\x00" * 20)
    assert onboarding.validate_backup(dump) == "dump"


def test_validate_backup_odooctl_dir_missing_db_dump(tmp_path):
    d = tmp_path / "backup_dir"
    d.mkdir()
    (d / "db.dump").write_bytes(b"x")
    # detect_format requires db.dump to exist for odooctl_dir detection at all;
    # remove it after "detecting" via a directory containing only meta.json to hit our extra check.
    (d / "db.dump").unlink()
    (d / "meta.json").write_text("{}")
    with pytest.raises(onboarding.InitError) as exc:
        onboarding.validate_backup(d)
    assert "Unrecognized backup format" in str(exc.value)


# ---------------------------------------------------------------------------
# Addon repo cloning
# ---------------------------------------------------------------------------


def test_clone_addon_repos_dry_run_makes_no_filesystem_changes(tmp_path):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/oca/queue.git#18.0"])

    results = onboarding.clone_addon_repos(custom_addons, specs, dry_run=True)

    assert results[0].status == "planned"
    assert not (custom_addons / "queue").exists()


def test_clone_addon_repos_skips_existing_destination(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    (custom_addons / "queue").mkdir(parents=True)
    specs = onboarding.parse_addon_repo_specs(["https://example.com/oca/queue.git"])

    called = []
    monkeypatch.setattr(onboarding.subprocess, "run", lambda *a, **k: called.append(a) or None)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "conflict"
    assert not called  # never even tried to clone


class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def test_clone_addon_repos_direct_single_module_layout(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/acme/my_addon.git#18.0"])

    def fake_run(cmd, capture_output=True):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True)
        (dest / "__manifest__.py").write_text("{'name': 'x'}")
        assert "--branch" in cmd and "18.0" in cmd
        return _FakeProc(0)

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "ok"
    assert results[0].modules == ["my-addon"]


def test_clone_addon_repos_collection_goes_direct_when_directory_is_empty(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/oca/queue.git"])

    def fake_run(cmd, capture_output=True):
        dest = tmp_path / "custom_addons" / ".queue.clone"
        dest.mkdir(parents=True)
        for mod in ("queue_job", "queue_job_cron"):
            (dest / mod).mkdir()
            (dest / mod / "__manifest__.py").write_text("{}")
        return _FakeProc(0)

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "direct"
    assert set(results[0].modules) == {"queue_job", "queue_job_cron"}
    assert "directly in custom_addons/" in results[0].message


def test_clone_addon_repos_collection_stays_nested_when_directory_is_not_empty(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    (custom_addons / "existing_addon").mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/oca/queue.git"])

    def fake_run(cmd, capture_output=True):
        dest = tmp_path / "custom_addons" / "queue"
        dest.mkdir(parents=True)
        for mod in ("queue_job", "queue_job_cron"):
            (dest / mod).mkdir()
            (dest / mod / "__manifest__.py").write_text("{}")
        return _FakeProc(0)

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "nested"
    assert set(results[0].modules) == {"queue_job", "queue_job_cron"}
    assert "nested one level deep" in results[0].message


def test_configure_nested_addon_paths_preserves_repo_and_updates_odoo_config(tmp_path):
    project = write_compose(tmp_path / "project")
    (project / "custom_addons").mkdir()
    config = project / "config"
    config.mkdir()
    (config / "odoo.conf").write_text("addons_path = /usr/lib/python3/dist-packages, /mnt/extra-addons\n")
    _, entry = parse_compose(project / "docker-compose.yml")
    spec = onboarding.parse_addon_repo_specs(["https://example.com/oca/queue.git"])[0]
    result = onboarding.AddonRepoResult(
        spec,
        "nested",
        project / "custom_addons" / "queue",
        ["queue_job", "queue_job_cron"],
        "nested modules",
    )

    onboarding.configure_nested_addon_paths(entry, [result])

    assert result.status == "configured"
    assert "2 module(s) visible" in result.message
    text = (config / "odoo.conf").read_text()
    assert "/mnt/extra-addons/queue" in text
    assert "/usr/lib/python3/dist-packages" in text


def test_clone_addon_repos_empty_repo_warns(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/acme/docs.git"])

    def fake_run(cmd, capture_output=True):
        Path(cmd[-1]).mkdir(parents=True)
        return _FakeProc(0)

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "empty"


def test_clone_addon_repos_failure_cleans_up_and_reports(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/acme/broken.git"])

    def fake_run(cmd, capture_output=True):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True)  # git sometimes leaves a partial dir behind
        return _FakeProc(128, stderr=b"fatal: repository not found")

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "error"
    assert "repository not found" in results[0].message
    assert not (custom_addons / "broken").exists()  # cleaned up, not left half-cloned


def test_clone_addon_repos_reports_requirements_txt(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/acme/needs_deps.git"])

    def fake_run(cmd, capture_output=True):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True)
        (dest / "__manifest__.py").write_text("{}")
        (dest / "requirements.txt").write_text("some-pkg\n")
        return _FakeProc(0)

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "ok"
    assert "requirements.txt" in results[0].message
    assert "rebuild" in results[0].message


def test_clone_addon_repos_missing_git(tmp_path, monkeypatch):
    custom_addons = tmp_path / "custom_addons"
    custom_addons.mkdir()
    specs = onboarding.parse_addon_repo_specs(["https://example.com/acme/x.git"])

    monkeypatch.setattr(onboarding.shutil, "which", lambda name: None)

    results = onboarding.clone_addon_repos(custom_addons, specs)

    assert results[0].status == "error"
    assert "git not found" in results[0].message


# ---------------------------------------------------------------------------
# Friendly error wrapping
# ---------------------------------------------------------------------------


def test_wrap_provision_error_adds_matching_hint():
    err = onboarding.wrap_provision_error(RuntimeError("Need --version or --template."))
    wrapped = onboarding.wrap_provision_error(RuntimeError("Need --version or --template."))
    assert wrapped.hint is not None
    assert "guided setup" in wrapped.hint
    assert err.nothing_changed is True


def test_wrap_provision_error_no_matching_hint_still_readable():
    wrapped = onboarding.wrap_provision_error(RuntimeError("something else entirely"))
    assert wrapped.hint is None
    assert str(wrapped) == "something else entirely\nNothing was changed."
