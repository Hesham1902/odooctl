from pathlib import Path

import pytest

from odooctl import compose, pull_workflow, registry


def _entry(tmp_path):
    project = tmp_path / "acme"
    project.mkdir()
    return {
        "path": str(project),
        "services": {"web": "web", "db": "db"},
        "ports": {"http": 8069},
        "db_user": "odoo",
    }


def _plan(options):
    return pull_workflow.PullPlan(
        "acme",
        "user@odoo.sh",
        None,
        None,
        None,
        "acme_pulled",
        options,
    )


def test_plan_pull_requires_a_target_and_mentions_save(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "load_pull_settings", lambda slug: {})

    with pytest.raises(pull_workflow.PullWorkflowError, match="--save"):
        pull_workflow.plan_pull("acme", pull_workflow.PullOptions())


def test_run_pull_respects_optional_restore_steps(tmp_path, monkeypatch):
    entry = _entry(tmp_path)
    options = pull_workflow.PullOptions(
        ssh_target="ssh://user@odoo.sh",
        database="acme_pulled",
        reset_admin=False,
        fix_icons=False,
        sanitize=False,
        overwrite=True,
        keep_download=True,
    )
    events = []
    progress = []

    monkeypatch.setattr(compose, "service_state", lambda *args: ("running", {}))
    monkeypatch.setattr(compose, "databases", lambda *args: [])
    monkeypatch.setattr(compose, "web_running", lambda *args: True)
    monkeypatch.setattr(compose, "run", lambda path, *args, **kwargs: events.append(args))
    monkeypatch.setattr(
        pull_workflow.pull,
        "find_remote_backup",
        lambda *args, **kwargs: {"sql_gz": "/backup/acme.sql.gz", "mirror": None},
    )

    def fake_download(path, port, remote, destination, **kwargs):
        assert kwargs["with_filestore"] is False
        assert callable(kwargs["progress"])
        bundle = Path(destination) / "acme"
        bundle.mkdir(parents=True)
        (bundle / "acme.sql.gz").write_bytes(b"not-a-real-gzip")
        kwargs["progress"]("download complete")
        return bundle

    monkeypatch.setattr(pull_workflow.pull, "download", fake_download)
    monkeypatch.setattr(
        pull_workflow.restore,
        "restore",
        lambda *args: {"filestore": False, "skipped_extensions": []},
    )
    monkeypatch.setattr(pull_workflow.icons, "fix_icons", lambda *args: events.append("icons"))
    monkeypatch.setattr(pull_workflow.admin, "reset_admin", lambda *args: events.append("admin"))
    monkeypatch.setattr(pull_workflow.sanitize, "sanitize", lambda *args: events.append("sanitize"))

    result = pull_workflow.run_pull(
        "acme",
        entry,
        options,
        progress=progress.append,
        plan=_plan(options),
    )

    assert result.database == "acme_pulled"
    assert result.filestore is False
    assert progress == ["[acme] looking for the latest backup on user@odoo.sh...", "[acme] found /backup/acme.sql.gz (dump only)", "[acme] skipping filestore (enable 'Include filestore' to include attachments)", "download complete", "[acme] downloaded acme/ (0.0 MB)", "[acme] stopping web for the restore...", "[acme] restoring into 'acme_pulled'...", "[!] menu icon repair skipped (the option is disabled)", "[acme] admin reset skipped; restored credentials were kept.", "[acme] sanitization skipped; the restored database was not neutralized.", "[acme] starting web back...", f"[acme] bundle kept at {entry['path']}/backups/pulled/acme", "[acme] done -> http://localhost:8069"]
    assert events == [("stop", "web"), ("start", "web")]


def test_run_pull_refuses_existing_database_without_overwrite(tmp_path, monkeypatch):
    entry = _entry(tmp_path)
    options = pull_workflow.PullOptions(ssh_target="ssh://user@odoo.sh")
    monkeypatch.setattr(compose, "service_state", lambda *args: ("running", {}))
    monkeypatch.setattr(compose, "databases", lambda *args: ["acme_pulled"])

    with pytest.raises(pull_workflow.ExistingDatabaseError, match="Replace existing database"):
        pull_workflow.run_pull("acme", entry, options, plan=_plan(options))
