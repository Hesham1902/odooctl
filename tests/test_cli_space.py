from pathlib import Path

from click.testing import CliRunner

from odooctl import cli


def _register(slug, tmp_path):
    from odooctl import registry

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


def test_df_reports_global_and_project(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(
        cli.space,
        "system_df",
        lambda: {
            "images": {"total": 5, "active": 2, "size_bytes": 12_000_000_000, "reclaim_bytes": None},
            "local volumes": {"total": 4, "active": 2, "size_bytes": 8_000_000_000, "reclaim_bytes": None},
            "build cache": {
                "total": 25,
                "active": 0,
                "size_bytes": 1_500_000_000,
                "reclaim_bytes": 1_500_000_000,
            },
        },
    )
    monkeypatch.setattr(
        cli.space,
        "dangling_images",
        lambda: [
            {"id": "abc123", "size_bytes": 640_000_000, "created": "2 weeks ago"},
        ],
    )
    monkeypatch.setattr(
        cli.compose, "find_built_image", lambda e, role: f"acme-{role}" if role == "web" else None
    )
    monkeypatch.setattr(cli.space, "image_identity", lambda ref: ("sha256:aaa", 1_900_000_000))
    monkeypatch.setattr(cli.space, "list_images", lambda: [])
    monkeypatch.setattr(cli.space, "project_volume_sizes", lambda slug: {f"{slug}_pgdata": 850_000_000})
    monkeypatch.setattr(cli.space, "bind_mounts", lambda entry: [])
    monkeypatch.setattr(cli.registry, "detect_version", lambda e: "18.0")

    result = CliRunner().invoke(cli.main, ["df"])
    assert result.exit_code == 0, result.output
    assert "GLOBAL DOCKER" in result.output
    assert "acme" in result.output
    assert "odoo 18.0" in result.output
    assert "1.9 GB" in result.output
    assert "not built" in result.output
    assert "850.0 MB" in result.output
    assert "total" in result.output
    assert entry["path"] in result.output


def test_df_marks_shared_images_and_savings(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    _register("bravo", tmp_path)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.space, "system_df", lambda: {})
    monkeypatch.setattr(cli.space, "dangling_images", lambda: [])

    refs = {}

    def fake_find(entry, role):
        slug = Path(entry["path"]).name
        refs[(slug, role)] = f"{slug}-{role}"
        return refs[(slug, role)]

    monkeypatch.setattr(cli.compose, "find_built_image", fake_find)

    ids = {
        ("acme", "web"): "sha256:same",
        ("bravo", "web"): "sha256:same",
        ("acme", "db"): "sha256:d1",
        ("bravo", "db"): "sha256:d2",
    }

    def fake_identity(ref):
        for (slug, role), r in refs.items():
            if r == ref:
                return ids[(slug, role)], 700_000_000
        return None, None

    monkeypatch.setattr(cli.space, "image_identity", fake_identity)
    monkeypatch.setattr(cli.space, "list_images", lambda: [])
    monkeypatch.setattr(cli.space, "bind_mounts", lambda e: [])

    result = CliRunner().invoke(cli.main, ["df"])
    assert result.exit_code == 0, result.output
    assert "(shared ×2)" in result.output
    assert "saved via layer sharing" in result.output


def test_df_lists_untracked_images(tmp_path, monkeypatch):
    _register("acme", tmp_path)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.space, "system_df", lambda: {})
    monkeypatch.setattr(cli.space, "dangling_images", lambda: [])
    monkeypatch.setattr(cli.compose, "find_built_image", lambda e, role: None)
    monkeypatch.setattr(
        cli.space,
        "list_images",
        lambda: [
            {"id": "x1", "tag": "odoo:16", "size_bytes": 1_600_000_000},
            {"id": "x2", "tag": None, "size_bytes": 999},  # dangling-ish, skipped
            {"id": None, "tag": "weird:none-tagged", "size_bytes": 5},
        ],
    )

    result = CliRunner().invoke(cli.main, ["df"])
    assert result.exit_code == 0, result.output
    assert "untracked" in result.output
    assert "odoo:16" in result.output
    assert "1 tagged image(s)" in result.output


def test_gc_dry_run_lists_plan_without_executing(tmp_path, monkeypatch):
    entry = _register("acme", tmp_path)
    (Path(entry["path"]) / "backups" / "odooctl" / "prod_20260101_000000").mkdir(parents=True)
    (Path(entry["path"]) / "backups" / "odooctl" / "prod_20260301_000000").mkdir(parents=True)
    (Path(entry["path"]) / "backups" / "odooctl" / "prod_20260301_000000" / "db.dump").write_bytes(b"x" * 100)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.space, "dangling_images", lambda: [])
    monkeypatch.setattr(cli.space, "system_df", lambda: {})
    monkeypatch.setattr(cli.space, "anonymous_volume_orphans", lambda: [])
    monkeypatch.setattr(cli.space.compose, "service_state", lambda path, svc: ("stopped", {}))

    executed = []
    monkeypatch.setattr(cli.space, "execute", lambda items, echo: executed.extend(items))

    result = CliRunner().invoke(cli.main, ["gc", "--keep-backups", "1"])
    assert result.exit_code == 0, result.output
    assert "Dry run only" in result.output
    assert "prod_20260101_000000" in result.output
    assert not executed

    result = CliRunner().invoke(cli.main, ["gc", "--keep-backups", "1", "--apply"])
    assert result.exit_code == 0, result.output
    assert len(executed) == 1
    assert executed[0].target.endswith("prod_20260101_000000")


def test_backup_prunes_older_snapshots(tmp_path, monkeypatch):
    entry = _register("bk", tmp_path)
    root = Path(entry["path"]) / "backups" / "odooctl"
    for stamp in ("20260101_000000", "20260201_000000"):
        (root / f"prod_{stamp}").mkdir(parents=True)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    class FakeProc:
        returncode = 0
        stdout = b""

    def fake_exec_service(*a, **kw):
        return FakeProc()

    monkeypatch.setattr(cli.compose, "exec_service", fake_exec_service)

    result = CliRunner().invoke(cli.main, ["backup", "bk", "-d", "prod", "--keep", "1"])
    assert result.exit_code == 0, result.output
    remaining = sorted(p.name for p in root.iterdir())
    assert len(remaining) == 1
    assert remaining[0].startswith("prod_2026")
    assert "pruned 2 older snapshot(s)" in result.output


def test_remove_stops_containers_keeps_shared_image(tmp_path, monkeypatch):
    _register("newclient", tmp_path)
    _register("cladex", tmp_path)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: ("running", {}))
    downs = []
    monkeypatch.setattr(cli.compose, "run", lambda path, *a, **kw: downs.append(a))
    monkeypatch.setattr(cli.compose, "find_built_image", lambda e, role: f"{Path(e['path']).name}-{role}")
    monkeypatch.setattr(cli.space, "image_identity", lambda ref: ("sha256:" + "a" * 64, 700_000_000))
    rmis = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: rmis.append(cmd))

    from odooctl import registry

    result = CliRunner().invoke(cli.main, ["remove", "newclient", "--images", "--yes"])
    assert result.exit_code == 0, result.output
    assert downs == [("down", "--remove-orphans", "-v")]
    assert not rmis
    assert "shared with" in result.output
    assert "removed from odooctl" in result.output
    assert "newclient" not in registry.get_projects()


def test_remove_removes_unshared_images(tmp_path, monkeypatch):
    _register("solo", tmp_path)

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: (None, {}))
    monkeypatch.setattr(
        cli.compose, "find_built_image", lambda e, role: {"web": "solo-web", "db": None}.get(role)
    )
    monkeypatch.setattr(cli.space, "image_identity", lambda ref: ("sha256:" + "b" * 64, 755_000_000))
    rmis = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: rmis.append(cmd))

    result = CliRunner().invoke(cli.main, ["remove", "solo", "--images", "--yes"])
    assert result.exit_code == 0, result.output
    assert rmis == [["docker", "rmi", "solo-web"]]
    assert "removed image solo-web" in result.output


def test_remove_purge_folder_deletes_everything(tmp_path, monkeypatch):
    entry = _register("solo", tmp_path)
    data_dir = Path(entry["path"]) / "data"
    data_dir.mkdir()
    (data_dir / "PG_VERSION").write_text("16")

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: (None, {}))

    result = CliRunner().invoke(cli.main, ["remove", "solo", "--purge-folder", "--yes"])
    assert result.exit_code == 0, result.output
    assert not Path(entry["path"]).exists()


def test_remove_purge_folder_asks_confirmation(tmp_path, monkeypatch):
    entry = _register("solo", tmp_path)
    Path(entry["path"], "x.txt").write_text("keep me")

    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "service_state", lambda p, s: (None, {}))

    result = CliRunner().invoke(cli.main, ["remove", "solo", "--purge-folder"], input="n\n")
    assert result.exit_code != 0 or "not confirmed" in result.output.lower() or result.exception
    assert Path(entry["path"]).exists()
