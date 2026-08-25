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


def test_detect_format_odoosh_raw_dir(tmp_path):
    d = tmp_path / "acme_daily"
    d.mkdir()
    (d / "acme_daily.sql.gz").write_bytes(b"\x1f\x8b\x08...")
    assert restore.detect_format(d) == "odoosh_raw"


def test_target_name_odoosh_raw_from_json_meta(tmp_path):
    import json

    d = tmp_path / "acme-prod-daily"
    d.mkdir()
    (d / "acme_daily.sql.gz").write_bytes(b"\x1f\x8b")
    (d / "acme_daily.json").write_text(json.dumps({"database": "acme_prod"}))
    assert restore.target_name(d, "odoosh_raw") == "acme_prod"


def test_target_name_odoosh_raw_no_meta(tmp_path):
    d = tmp_path / "x"
    d.mkdir()
    (d / "x.sql.gz").write_bytes(b"\x1f\x8b")
    assert restore.target_name(d, "odoosh_raw") is None


def test_sql_chunks_filters_restrict(tmp_path):
    import gzip

    gz = tmp_path / "d.sql.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write("-- header\n\\restrict abc123\nSELECT 1;\n\\unrestrict\nSELECT 2;\n")
    chunks = list(restore._sql_chunks(gz))
    text = b"".join(chunks).decode()
    assert "restrict" not in text
    assert "SELECT 1;" in text and "SELECT 2;" in text


def test_sql_chunks_skips_unavailable_extensions(tmp_path):
    import gzip

    gz = tmp_path / "d.sql.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write(
            "CREATE EXTENSION IF NOT EXISTS pg_trgm;\n"
            "CREATE EXTENSION IF NOT EXISTS vector;\n"
            "COMMENT ON EXTENSION vector IS 'ai stuff';\n"
            "COMMENT ON EXTENSION pg_trgm IS 'trigram';\n"
            "CREATE TABLE x (id int);\n"
        )
    skipped = []
    text = b"".join(restore._sql_chunks(gz, available={"pg_trgm", "plpgsql"}, skipped=skipped)).decode()
    assert "vector" not in text
    assert "CREATE EXTENSION IF NOT EXISTS pg_trgm;" in text
    assert "COMMENT ON EXTENSION pg_trgm" in text
    assert "CREATE TABLE x" in text
    assert skipped == ["vector", "vector"]


def test_sql_chunks_no_filtering_when_availability_unknown(tmp_path):
    import gzip

    gz = tmp_path / "d.sql.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write("CREATE EXTENSION IF NOT EXISTS vector;\n")
    text = b"".join(restore._sql_chunks(gz, available=None)).decode()
    assert "vector" in text


def test_restore_odoosh_raw_replays_sql_and_filestore(tmp_path, monkeypatch):
    import gzip

    d = tmp_path / "bundle"
    d.mkdir()
    sql = "-- Odoo dump\nSELECT 1;"
    with gzip.open(d / "dump.sql.gz", "wb") as fh:
        fh.write(sql.encode())
    fs = d / "home" / "odoo" / "data" / "filestore" / "visionprod"
    fs.mkdir(parents=True)
    blob = fs / "ab" / "abc123"
    blob.parent.mkdir()
    blob.write_text("binary")

    exec_calls = []
    stream_calls = []

    class FakeProc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_exec(project_path, service, *cmd, capture=False, stdin_file=None, **kw):
        exec_calls.append((service, cmd))
        return FakeProc()

    def fake_stream(project_path, args, chunks):
        data = b"".join(chunks)
        stream_calls.append((args, data))
        return 0

    monkeypatch.setattr(restore.compose, "exec_service", fake_exec)

    def fake_available(project_path, entry):
        return {"pg_trgm", "plpgsql", "vector"}

    monkeypatch.setattr(restore, "_available_extensions", fake_available)
    monkeypatch.setattr(restore.compose, "run_with_stdin_stream", fake_stream)

    entry = {"services": {"web": "web", "db": "db"}, "db_user": "odoo"}
    info = restore.restore("/proj", entry, d, "restored_db")

    assert info == {"db": "restored_db", "filestore": True, "format": "odoosh_raw", "skipped_extensions": []}
    assert len(stream_calls) == 1
    args, data = stream_calls[0]
    assert args[:3] == ["exec", "-T", "db"]
    assert "-v" in args and "ON_ERROR_STOP=1" in args
    idx = list(args).index("-d")
    assert args[idx + 1] == "restored_db"
    assert b"SELECT 1;" in data  # decompressed SQL reached psql
    tar_calls = [c for c in exec_calls if c[0] == "web"]
    assert tar_calls and "mkdir -p /var/lib/odoo/filestore/restored_db" in tar_calls[0][1][-1]


def test_dump_create_target_detected():

    p = tmp_header_file(
        "--\n-- PostgreSQL database dump\n--\n\n"
        'CREATE DATABASE "08-10-2026" WITH TEMPLATE = template0;\n\n'
        '\\connect "08-10-2026"\n'
        "SET default_tablespace = '';\n"
    )
    assert restore._dump_create_target(p) == "08-10-2026"


def test_dump_create_target_plain_dump_returns_none():

    p = tmp_header_file("-- plain dump\nCREATE TABLE x (id int);\n")
    assert restore._dump_create_target(p) is None


def tmp_header_file(text):
    import gzip
    from pathlib import Path

    p = Path("/tmp") / f"hdr_{abs(hash(text))}.sql.gz"
    with gzip.open(p, "wt") as fh:
        fh.write(text)
    return p


def test_restore_odoosh_raw_create_style_renames(tmp_path, monkeypatch):
    import gzip

    d = tmp_path / "bundle"
    d.mkdir()
    header = (
        '-- dump\nCREATE DATABASE "prod-daily" WITH TEMPLATE = template0;\n'
        '\\connect "prod-daily"\nCOPY public.x (a) FROM stdin;\n'
    )
    with gzip.open(d / "dump.sql.gz", "wb") as fh:
        fh.write(header.encode())

    exec_calls = []
    stream_calls = []

    class FakeProc:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_exec(project_path, service, *cmd, capture=False, stdin_file=None, **kw):
        exec_calls.append((service, cmd))
        return FakeProc()

    def fake_stream(project_path, args, chunks):
        b"".join(chunks)
        stream_calls.append(args)
        return 0

    monkeypatch.setattr(restore.compose, "exec_service", fake_exec)
    monkeypatch.setattr(restore, "_available_extensions", lambda p, e: {"pg_trgm", "plpgsql"})
    monkeypatch.setattr(restore.compose, "run_with_stdin_stream", fake_stream)

    entry = {"services": {"web": "web", "db": "db"}, "db_user": "odoo"}
    info = restore.restore("/proj", entry, d, "acme_prod")

    assert info["format"] == "odoosh_raw"
    assert info["skipped_extensions"] == []
    # replay went into postgres so CREATE DATABASE can run
    assert len(stream_calls) == 1
    args = stream_calls[0]
    idx = list(args).index("-d")
    assert args[idx + 1] == "postgres"
    # backends are terminated before the rename
    terminates = [c for c in exec_calls if any("pg_terminate_backend" in str(a) for a in c[1])]
    assert terminates
    renames = [c for c in exec_calls if any("ALTER DATABASE" in str(a) for a in c[1])]
    assert renames and 'ALTER DATABASE "prod-daily" RENAME TO "acme_prod"' in renames[0][1][-1]
