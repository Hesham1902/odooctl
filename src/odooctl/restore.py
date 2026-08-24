import gzip
import json
import re
import tarfile
import tempfile
import zipfile
from pathlib import Path

from . import compose, testing


def detect_format(path: Path):
    if path.is_dir() and (path / "db.dump").exists():
        return "odooctl_dir"
    if path.is_dir() and list(path.glob("*.sql.gz")):
        return "odoosh_raw"
    if path.is_file() and path.suffix.lower() == ".zip":
        return "zip"
    if path.is_file() and path.suffix.lower() in (".dump", ".backup"):
        return "dump"
    return None


def target_name(src: Path, fmt, override=None):
    if override:
        return override
    if fmt == "odooctl_dir":
        meta = src / "meta.json"
        if meta.exists():
            data = json.loads(meta.read_text())
            if data.get("database"):
                return data["database"]
        return src.name.split("_")[0]
    if fmt == "odoosh_raw":
        for meta in sorted(src.glob("*.json")):
            try:
                data = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for key in ("database", "db", "dbname"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return None
    return None


def _recreate(project_path, entry, db):
    user = entry.get("db_user", "odoo")
    testing.drop_db_if_exists(project_path, entry, db)
    compose.exec_service(project_path, entry["services"]["db"], "createdb", "-U", user, db)


def _untar_into_filestore(project_path, entry, db, tar_path, archive_contains_db_dir):
    sub = f"/var/lib/odoo/filestore/{db}" if not archive_contains_db_dir else "/var/lib/odoo/filestore"
    with open(tar_path, "rb") as fh:
        compose.exec_service(
            project_path, entry["services"]["web"], "sh", "-c",
            f"mkdir -p {sub} && tar xzf - -C {sub}",
            capture=False, stdin_file=fh,
        )


def _zip_filestore_to_tar(filestore_dir: Path):
    tmp = Path(tempfile.mkdtemp(prefix="odooctl_fs_"))
    tar_path = tmp / "filestore.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for child in filestore_dir.iterdir():
            tar.add(child, arcname=child.name)
    return tar_path


def _dump_create_target(gz: Path):
    """If the dump is pg_dump --create style (CREATE DATABASE + \\connect),
    return the db name it will build itself; else None."""
    pattern = re.compile(r'^\\connect\s+(?:"([^"]+)"|(\S+))')
    try:
        with gzip.open(gz, "rt", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 500:
                    break
                m = pattern.match(line)
                if m:
                    return m.group(1) or m.group(2)
    except Exception:
        pass
    return None


def _quote_ident(name):
    if not name or '"' in name:
        raise ValueError(f"Invalid database name: {name!r}")
    return f'"{name}"'


def _terminate_backends(project_path, entry, db):
    user = entry.get("db_user", "odoo")
    compose.exec_service(
        project_path, entry["services"]["db"], "psql", "-U", user, "-d", "postgres",
        "-c",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{db}' AND pid <> pg_backend_pid()",
        check=False,
    )


_EXT_LINE_RE = re.compile(
    r'^(CREATE EXTENSION(?: IF NOT EXISTS)?|COMMENT ON EXTENSION)\s+"?([a-zA-Z0-9_]+)"?')


def _available_extensions(project_path, entry):
    """Extensions installable in the local postgres (None = unknown, don't filter)."""
    try:
        proc = compose.exec_service(
            project_path, entry["services"]["db"], "psql",
            "-U", entry.get("db_user", "odoo"), "-d", "postgres",
            "-Atc", "SELECT name FROM pg_available_extensions",
        )
        return set(proc.stdout.decode().split())
    except compose.DockerError:
        return None


def _sql_chunks(gz: Path, available=None, skipped=None):
    """Yield the dump's SQL as encoded lines, decompressed on the fly.

    - \\restrict / \\unrestrict (psql >= 17) are dropped: older psql aborts on them.
    - CREATE EXTENSION / COMMENT ON EXTENSION lines for extensions the local
      postgres cannot install are dropped (odoo.sh pre-installs extras like
      pgvector; skipping matches what a manual psql restore silently did).
    """
    with gzip.open(gz, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("\\restrict") or line.startswith("\\unrestrict"):
                continue
            if available is not None:
                m = _EXT_LINE_RE.match(line)
                if m and m.group(2) not in available:
                    if skipped is not None:
                        skipped.append(m.group(2))
                    continue
            yield line.encode("utf-8")


def restore(project_path, entry, src: Path, db):
    fmt = detect_format(src)
    if not fmt:
        raise ValueError(f"Unrecognized backup format: {src}")

    user = entry.get("db_user", "odoo")
    db_svc = entry["services"]["db"]
    _recreate(project_path, entry, db)

    if fmt == "dump":
        with open(src, "rb") as fh:
            compose.exec_service(
                project_path, db_svc, "pg_restore", "-U", user, "--no-owner", "-d", db,
                capture=False, stdin_file=fh,
            )
        return {"db": db, "filestore": False, "format": fmt}

    if fmt == "odooctl_dir":
        with open(src / "db.dump", "rb") as fh:
            compose.exec_service(
                project_path, db_svc, "pg_restore", "-U", user, "--no-owner", "-d", db,
                capture=False, stdin_file=fh,
            )
        fs = src / "filestore.tar.gz"
        if fs.exists():
            _untar_into_filestore(project_path, entry, db, fs, archive_contains_db_dir=True)
        return {"db": db, "filestore": fs.exists(), "format": fmt}

    if fmt == "odoosh_raw":
        gz = sorted(src.glob("*.sql.gz"))[0]
        dump_db = _dump_create_target(gz)
        if dump_db:
            # pg_dump --create style: the dump builds and connects to its own db.
            # Let it, then rename to the requested name.
            testing.drop_db_if_exists(project_path, entry, dump_db)
            testing.drop_db_if_exists(project_path, entry, db)
            replay_into = "postgres"
        else:
            _recreate(project_path, entry, db)
            replay_into = db
        skipped_ext: list[str] = []
        available = _available_extensions(project_path, entry)
        compose.run_with_stdin_stream(
            project_path,
            ["exec", "-T", db_svc, "psql", "-U", user, "-d", replay_into,
             "-v", "ON_ERROR_STOP=1"],
            _sql_chunks(gz, available=available, skipped=skipped_ext),
        )
        if dump_db and dump_db != db:
            _terminate_backends(project_path, entry, dump_db)
            compose.exec_service(
                project_path, db_svc, "psql", "-U", user, "-d", "postgres",
                "-c", f"ALTER DATABASE {_quote_ident(dump_db)} RENAME TO {_quote_ident(db)}",
            )
        fs_dir = next((d for d in src.rglob("filestore") if d.is_dir()), None)
        has_fs = fs_dir and any(fs_dir.iterdir())
        if has_fs:
            tar_path = _zip_filestore_to_tar(fs_dir)
            _untar_into_filestore(project_path, entry, db, tar_path,
                                  archive_contains_db_dir=False)
        return {"db": db, "filestore": bool(has_fs), "format": fmt,
                "skipped_extensions": sorted(set(skipped_ext))}

    tmp = Path(tempfile.mkdtemp(prefix="odooctl_restore_"))
    with zipfile.ZipFile(src) as zf:
        zf.extractall(tmp)

    sql_files = sorted(tmp.rglob("*.sql"))
    if not sql_files:
        raise ValueError(f"No .sql found inside zip {src}")
    dump_sql = next((f for f in sql_files if f.name == "dump.sql"), sql_files[0])

    with open(dump_sql, "rb") as fh:
        compose.exec_service(
            project_path, db_svc, "psql", "-U", user, "-d", db,
            "-v", "ON_ERROR_STOP=1",
            capture=False, stdin_file=fh,
        )

    fs_dir = next((d for d in tmp.rglob("filestore") if d.is_dir()), None)
    if fs_dir and any(fs_dir.iterdir()):
        tar_path = _zip_filestore_to_tar(fs_dir)
        _untar_into_filestore(project_path, entry, db, tar_path, archive_contains_db_dir=False)
        return {"db": db, "filestore": True, "format": fmt}
    return {"db": db, "filestore": False, "format": fmt}


def zip_server_version(src: Path):
    try:
        with zipfile.ZipFile(src) as zf:
            name = next((n for n in zf.namelist() if n.endswith("manifest.json")), None)
            if not name:
                return None
            data = json.loads(zf.read(name))
            for key in ("server_version", "odoo_version", "version"):
                value = data.get(key)
                if value:
                    m = re.match(r"(\d+)", str(value))
                    if m:
                        return f"{m.group(1)}.0"
    except Exception:
        pass
    return None
