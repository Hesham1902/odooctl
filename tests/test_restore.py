import zipfile

from odooctl import restore


def test_detect_format_odooctl_dir(tmp_path):
    d = tmp_path / "mydb_20260821_101010"
    d.mkdir()
    (d / "db.dump").write_bytes(b"x")
    assert restore.detect_format(d) == "odooctl_dir"


def test_detect_format_dump_and_zip(tmp_path):
    dump = tmp_path / "x.dump"
    dump.write_bytes(b"x")
    z = tmp_path / "backup.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("dump.sql", "SELECT 1;")
    assert restore.detect_format(dump) == "dump"
    assert restore.detect_format(z) == "zip"


def test_detect_format_unknown(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hi")
    assert restore.detect_format(other) is None
    assert restore.detect_format(tmp_path) is None


def test_target_name_override_wins(tmp_path):
    d = tmp_path / "olddb_2026"
    d.mkdir()
    assert restore.target_name(d, "odooctl_dir", override="newdb") == "newdb"


def test_target_name_from_meta_json(tmp_path):
    import json

    d = tmp_path / "whatever"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"database": "vision-prod"}))
    assert restore.target_name(d, "odooctl_dir") == "vision-prod"


def test_target_name_fallback_from_folder_prefix(tmp_path):
    d = tmp_path / "mydb_20260821_101010"
    d.mkdir()
    assert restore.target_name(d, "odooctl_dir") == "mydb"


def _make_odoo_sh_zip(path, version):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "manifest.json",
            f'{{"server_version": "{version}", "database": "client"}}',
        )
        zf.writestr("dump.sql", "-- Odoo dump")
        zf.writestr("filestore/ab/abc123", "binary")


def test_zip_server_version_detected():
    z = "/tmp/nonexistent_used_by_zipfile_only.zip"
    p = __import__("pathlib").Path(z)
    _make_odoo_sh_zip(p, "18.0-23.0")
    assert restore.zip_server_version(p) == "18.0"


def test_zip_server_version_no_manifest(tmp_path):
    p = tmp_path / "plain.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("dump.sql", "SELECT 1;")
    assert restore.zip_server_version(p) is None


def test_zip_server_version_corrupt_zip(tmp_path):
    p = tmp_path / "bad.zip"
    p.write_bytes(b"not a zip at all")
    assert restore.zip_server_version(p) is None
