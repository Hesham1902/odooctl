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
