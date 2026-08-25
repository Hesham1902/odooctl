import os
import time

import yaml

from odooctl import space

LIVE_DF_LINES = (
    '{"Active":"2","Reclaimable":"3.147GB (44%)","Size":"7.083GB","TotalCount":"8","Type":"Images"}\n'
    '{"Active":"2","Reclaimable":"0B (0%)","Size":"77.82kB","TotalCount":"2","Type":"Containers"}\n'
    '{"Active":"0","Reclaimable":"845.5MB (100%)","Size":"845.5MB","TotalCount":"1","Type":"Local Volumes"}\n'
    '{"Active":"0","Reclaimable":"4.132GB","Size":"11.22GB","TotalCount":"99","Type":"Build Cache"}\n'
)


def test_parse_system_df_handles_modern_docker_keys():
    totals = space.parse_system_df(LIVE_DF_LINES)
    images = totals["images"]
    assert images["total"] == 8
    assert images["active"] == 2
    assert images["size_bytes"] == 7_083_000_000
    assert images["reclaim_bytes"] == 3_147_000_000
    build = totals["build cache"]
    assert build["total"] == 99
    assert build["size_bytes"] == 11_220_000_000
    assert build["reclaim_bytes"] == 4_132_000_000  # no (%) suffix - regression!
    assert totals["local volumes"]["total"] == 1


def test_parse_system_df_handles_legacy_keys():
    text = '{"Type":"Images","Total":3,"Active":1,"Size":"1GB","Reclaimable":"500MB (50%)"}'
    totals = space.parse_system_df(text)
    assert totals["images"] == {
        "total": 3,
        "active": 1,
        "size_bytes": 1_000_000_000,
        "reclaim_bytes": 500_000_000,
    }


def test_parse_system_df_garbage_lines_skipped():
    totals = space.parse_system_df('not json\n\n{"Type":"Images","Size":"5B"}\n')
    assert totals["images"]["size_bytes"] == 5
    assert len(totals) == 1


def test_parse_bytes_handles_docker_units():
    assert space.parse_bytes("899B") == 899
    assert space.parse_bytes("12kB") == 12_000
    assert space.parse_bytes("1.5MB") == 1_500_000
    assert space.parse_bytes("1.234GB") == 1_234_000_000
    assert space.parse_bytes("2TB") == 2_000_000_000_000
    assert space.parse_bytes("N/A") is None
    assert space.parse_bytes("") is None


def test_parse_bytes_kibibytes():
    assert space.parse_bytes("1kiB") == 1024
    assert space.parse_bytes("2MiB") == 2 * 1024**2


def test_fmt_bytes_round_trip():
    text = space.fmt_bytes(1_234_000_000)
    assert text == "1.2 GB"
    assert space.fmt_bytes(None) == "?"
    assert space.fmt_bytes(512) == "512 B"


def test_fmt_then_parse_round_trips_within_unit():
    original = 345_600_000
    parsed = space.parse_bytes(space.fmt_bytes(original))
    assert abs(parsed - original) < original * 0.01


def test_du_bytes_sums_files_and_skips_symlinks(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "big.bin").write_bytes(b"x" * 1000)
    (tmp_path / "b.txt").write_bytes(b"y" * 250)
    try:
        os.symlink("/etc/hostname", tmp_path / "link.txt")
    except OSError:
        pass
    assert space.du_bytes(tmp_path) == 1250
    assert space.du_bytes(tmp_path / "b.txt") == 250
    assert space.du_bytes(tmp_path / "missing") == 0


def test_backup_groups_splits_by_db_prefix(tmp_path):
    root = tmp_path / "odooctl"
    for name in ("prod_20260101_000000", "prod_20260301_000000", "test_x_20260201_000000"):
        (root / name).mkdir(parents=True)
    (root / "not-a-snapshot").mkdir()

    groups = space.backup_groups(root)
    assert sorted(groups) == ["prod", "test_x"]
    assert len(groups["prod"]) == 2


def test_plan_backup_prunes_keeps_newest_per_db(tmp_path):
    root = tmp_path / "odooctl"
    names = [
        "prod_20260101_000000",
        "prod_20260201_000000",
        "prod_20260301_000000",
        "demo_20260105_000000",
    ]
    for n in names:
        (root / n).mkdir(parents=True)

    doomed = space.plan_backup_prunes(root, keep=2)
    doomed_names = [p.name for p in doomed]
    assert doomed_names == ["prod_20260101_000000"]

    all_doomed = space.plan_backup_prunes(root, keep=0)
    assert len(all_doomed) == 4


def test_plan_log_prunes_keeps_newest_by_mtime(tmp_path):
    logs = tmp_path / "test_logs"
    logs.mkdir()
    old, mid, new = logs / "a.log", logs / "b.log", logs / "c.log"
    old.write_text("old")
    time.sleep(0.02)
    mid.write_text("mid")
    time.sleep(0.02)
    new.write_text("new")

    doomed = space.plan_log_prunes(logs, keep=1)
    assert [p.name for p in doomed] == ["a.log", "b.log"]
    assert space.plan_log_prunes(logs, keep=3) == []
    assert space.plan_log_prunes(logs / "missing", keep=1) == []


def test_gc_item_size_label():
    item = space.GCItem(kind="dir", description="x", target="/tmp/x", bytes_free=1500)
    assert item.size_label == "1.5 kB"
    assert space.GCItem(kind="dir", description="x", target="y").size_label == "-"


def test_execute_unknown_kind_raises():
    import pytest

    item = space.GCItem(kind="bogus", description="x", target="y")
    with pytest.raises(space.SpaceError):
        space.execute([item])


def _compose_entry(tmp_path, version="18"):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    data = {
        "services": {
            "web": {
                "build": {"dockerfile": "odoo.Dockerfile"},
                "volumes": [
                    "./home/odoo/.local/share/Odoo:/var/lib/odoo",
                    "./config:/etc/odoo",
                    f"/Users/dev/_odoo_addons/odoo-{version}.0/odoo/addons:/mnt/enterprise",
                ],
            },
            "db": {
                "build": {"dockerfile": "postgres.Dockerfile"},
                "volumes": [
                    {"type": "bind", "source": "./data", "target": "/var/lib/postgresql/data"},
                    "named_volume_x:/var/lib/postgresql/other",
                ],
            },
        }
    }
    (project_dir / "docker-compose.yml").write_text(yaml.safe_dump(data))
    (project_dir / "data").mkdir()
    odoo_dir = project_dir / "home" / "odoo" / ".local" / "share" / "Odoo"
    odoo_dir.mkdir(parents=True)
    return {
        "compose_file": str(project_dir / "docker-compose.yml"),
        "path": str(project_dir),
    }, project_dir


def test_bind_mounts_finds_data_dirs_only(tmp_path):
    entry, project_dir = _compose_entry(tmp_path)
    binds = space.bind_mounts(entry)

    found = {(str(p.relative_to(project_dir)), label) for p, label in binds}
    assert found == {
        ("home/odoo/.local/share/Odoo", "odoo data"),
        ("data", "pg data"),
    }


def test_bind_mounts_skips_missing_and_broken_compose(tmp_path):
    entry, _ = _compose_entry(tmp_path)
    import shutil as sh

    sh.rmtree(tmp_path / "proj" / "home")
    assert all("Odoo" not in str(p) for p, _ in space.bind_mounts(entry))

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "docker-compose.yml").write_text(": : broken [")
    assert space.bind_mounts({"compose_file": str(bad / "docker-compose.yml"), "path": str(bad)}) == []


def test_group_shared_image_usage_counts_users():
    usages = [
        ("a", "web", "a-web", "sha:same", 700),
        ("b", "web", "b-web", "sha:same", 700),
        ("a", "db", "a-db", "sha:d1", 100),
    ]
    groups = space.group_shared_image_usage(usages)
    assert len(groups["sha:same"]["users"]) == 2
    assert groups["sha:same"]["size"] == 700
    assert len(groups["sha:d1"]["users"]) == 1


def test_filter_untracked_keeps_only_unused_tagged():
    images = [
        {"id": "used", "tag": "acme-web", "size_bytes": 1},
        {"id": "free1", "tag": "odoo:16", "size_bytes": 10},
        {"id": "free2", "tag": None, "size_bytes": 20},  # untagged -> skip
        {"id": None, "tag": "weird", "size_bytes": 30},  # no id -> skip
        {"id": "free3", "tag": "old:1", "size_bytes": 40},
        {"id": "free3", "tag": "old:also", "size_bytes": 40},  # same id dedupe
    ]
    out = space.filter_untracked(images, referenced_ids={"used"})
    assert [i["id"] for i in out] == ["free1", "free3"]


def test_filter_untracked_matches_short_and_long_ids():
    """docker inspect yields sha256:<64hex>; docker images --json yields 12 chars."""
    images = [
        {"id": "1a2b3c4d5e6f", "tag": "cladex-internal-web:latest", "size_bytes": 755_000_000},
        {"id": "9f8e7d6c5b4a", "tag": "acme-web:latest", "size_bytes": 3_300_000_000},
    ]
    referenced = {"sha256:" + "1a2b3c4d5e6f" + "0" * 52}
    out = space.filter_untracked(images, referenced)
    assert [i["tag"] for i in out] == ["acme-web:latest"]


def test_anonymous_volume_orphans_filters_hash_names(monkeypatch):
    monkeypatch.setattr(
        space,
        "_docker",
        lambda *a, **kw: (
            '{"Name":"2a6c2b8ba58882d6e121ee0dad1b75e92e882f580c6addc1543eb1d215832e15","Driver":"local"}\n'
            '{"Name":"cladex_pgdata","Driver":"local"}\n'
        ).encode(),
    )
    sizes = {
        "2a6c2b8ba58882d6e121ee0dad1b75e92e882f580c6addc1543eb1d215832e15": 845_500_000,
        "cladex_pgdata": 178_900_000,
    }
    monkeypatch.setattr(space, "all_volume_sizes", lambda: sizes)

    orphans = space.anonymous_volume_orphans()
    assert len(orphans) == 1
    assert orphans[0]["size_bytes"] == 845_500_000


def test_execute_runs_rmi_and_images_before_builder(monkeypatch):
    order = []
    monkeypatch.setattr(space, "_docker", lambda *a: order.append(a[0] if a[0] != "rmi" else "rmi"))
    items = [
        space.GCItem(kind="builder", description="cache", target="x"),
        space.GCItem(kind="rmi", description="old image", target="imgid"),
        space.GCItem(kind="images", description="dangling", target="x"),
    ]
    space.execute(items)
    assert order == ["rmi", "image", "builder"]


def test_execute_removes_orphan_volume(monkeypatch):
    calls = []
    monkeypatch.setattr(space, "_docker", lambda *a: calls.append(a))
    item = space.GCItem(
        kind="volume",
        description="orphan anonymous volume",
        target="2a6c" + "0" * 60,
        bytes_free=845_500_000,
    )
    freed = space.execute([item])
    assert freed == 845_500_000
    assert calls == [("volume", "rm", "2a6c" + "0" * 60)]
