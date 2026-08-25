import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import compose, testing


class SpaceError(RuntimeError):
    pass


# ---------------------------------------------------------------- pure helpers


def du_bytes(path):
    root = Path(path)
    if root.is_symlink():
        return 0
    if root.is_file():
        try:
            return root.stat().st_size
        except OSError:
            return 0
    total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                else:
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


_SIZE_RE = re.compile(r"^([\d.]+)\s*([kKmMgGtTpP]?)(i?)B?$")
_UNIT_EXP = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}


def parse_bytes(text):
    """'1.234GB' -> 1234000000. Returns None when unparseable."""
    m = _SIZE_RE.match(str(text).strip())
    if not m:
        return None
    value = float(m.group(1))
    exp = _UNIT_EXP[m.group(2).lower()]
    mult = 1024**exp if m.group(3) else 1000**exp
    return int(round(value * mult))


def fmt_bytes(num):
    if num is None:
        return "?"
    size = float(num)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(size) < 1000 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def backup_groups(backup_root: Path):
    """Map db-name -> [dirs] under backups/odooctl (dirs named '<db>_<YYYYmmdd>_<HHMMSS>')."""
    groups = {}
    root = Path(backup_root)
    if not root.is_dir():
        return groups
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        parts = child.name.rsplit("_", 2)
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        groups.setdefault(parts[0], []).append(child)
    return groups


def plan_backup_prunes(backup_root: Path, keep):
    """Newest `keep` snapshots per database survive; the rest are returned for deletion."""
    doomed = []
    for dirs in backup_groups(backup_root).values():
        ordered = sorted(dirs, key=lambda p: p.name)
        doomed.extend(ordered[:-keep] if keep > 0 else ordered)
    return sorted(doomed, key=lambda p: p.name)


def plan_log_prunes(logs_dir: Path, keep):
    """Keep the newest `keep` log files overall."""
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        return []
    files = [p for p in logs_dir.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[:-keep] if keep > 0 else files


def newest_stamps(dirs):
    return sorted(p.name.rsplit("_", 1)[-1] for p in dirs)


# ------------------------------------------------------------- docker queries


def _docker(*args, timeout=120):
    cmd = ["docker", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise SpaceError("Docker not found.") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpaceError(f"docker {' '.join(args[:2])} timed out") from exc
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace").strip()
        raise SpaceError(stderr or f"docker {' '.join(args[:2])} failed")
    return proc.stdout or b""


def _reclaim_bytes(value):
    """'3.147GB (44%)' -> 3147000000; '4.132GB' -> same idea. None if unparseable."""
    if value is None:
        return None
    text = str(value)
    if "(" in text:
        text = text.split("(")[0].strip()
    return parse_bytes(text)


def parse_system_df(text):
    """Parse `docker system df --format '{{json .}}'` lines.

    Handles both old ('Total', 'Active') and modern Docker ('TotalCount',
    'ActiveCount', 'Cleanable') key sets; numeric fields may be int or str.
    """
    totals = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = str(row.get("Type", "")).lower()

        def count(*keys):
            for k in keys:
                v = row.get(k)
                if v is None:
                    continue
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
            return None

        reclaim = _reclaim_bytes(row.get("Reclaimable"))
        if reclaim is None:
            reclaim = _reclaim_bytes(row.get("Cleanable"))
        totals[kind] = {
            "total": count("Total", "TotalCount"),
            "active": count("Active", "ActiveCount", "InUse"),
            "size_bytes": parse_bytes(row.get("Size")),
            "reclaim_bytes": reclaim,
        }
    return totals


def system_df():
    """Totals from `docker system df --format json`. Best effort; {} on parse trouble."""
    out = _docker("system", "df", "--format", "{{json .}}").decode()
    return parse_system_df(out)


def dangling_images():
    out = _docker("images", "-f", "dangling=true", "--format", "{{json .}}").decode()
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return [
        {
            "id": r.get("ID"),
            "size_bytes": parse_bytes(r.get("Size")),
            "created": r.get("CreatedSince"),
        }
        for r in rows
    ]


def image_size(ref):
    out = _docker("image", "inspect", ref, "--format", "{{.Size}}").decode().strip()
    try:
        return int(out)
    except ValueError:
        return None


def image_identity(ref):
    """(image_id, size_bytes) for one ref; (None, None) when missing."""
    out = _docker("image", "inspect", ref, "--format", "{{.Id}} {{.Size}}").decode().split()
    if len(out) != 2:
        return None, None
    try:
        return out[0], int(out[1])
    except ValueError:
        return out[0], None


def list_images():
    """All local images as [{'id','tag','size_bytes'}] (untagged entries have tag=None)."""
    out = _docker("images", "-a", "--format", "{{json .}}").decode()
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        repo, tag = r.get("Repository"), r.get("Tag")
        rows.append(
            {
                "id": r.get("ID"),
                "tag": f"{repo}:{tag}" if repo and tag and tag != "<none>" else None,
                "size_bytes": parse_bytes(r.get("Size")),
            }
        )
    return rows


def short_id(image_id):
    """Normalize docker image ids: 'sha256:abcdef...' -> 12-char form matching `docker images`."""
    return str(image_id or "").replace("sha256:", "")[:12]


def filter_untracked(images, referenced_ids):
    """Tagged images whose ID no registered project uses (untagged/dangling excluded).

    Accepts mixed id formats - `docker image inspect` yields 'sha256:<64 hex>'
    while `docker images --format json` yields the short 12-char form.
    """
    referenced = {short_id(i) for i in referenced_ids}
    seen = set()
    out = []
    for img in images:
        sid = short_id(img["id"])
        if not sid or not img["tag"]:
            continue
        if sid in referenced or sid in seen:
            continue
        seen.add(sid)
        out.append(img)
    return out


def group_shared_image_usage(usages):
    """[(slug, role, ref, id, size)] -> {id: {'refs': [...], 'size': bytes, 'users': [(slug, role)]}}"""
    groups = {}
    for slug, role, ref, img_id, size in usages:
        g = groups.setdefault(img_id, {"size": size, "users": []})
        g["users"].append((slug, role))
    return groups


BIND_TARGET_LABELS = (
    ("/var/lib/postgresql/data", "pg data"),
    ("/var/lib/postgresql/pgdata", "pg data"),
    ("/var/lib/postgresql", "pg data"),
    ("/var/lib/odoo", "odoo data"),
)


def bind_mounts(entry):
    """[(host_abs_path, label)] from the compose file's bind-mounted volumes.

    Only mounts whose container target looks like real data (postgres / odoo
    datadirs) are returned - config/addons binds don't matter for disk pressure.
    """
    compose_path = Path(entry["compose_file"])
    try:
        data = yaml.safe_load(compose_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    project_dir = Path(entry["path"])
    result = []
    for svc in (data.get("services") or {}).values():
        for vol in svc.get("volumes") or []:
            if isinstance(vol, dict):
                if vol.get("type") not in (None, "bind"):
                    continue
                host, target = vol.get("source"), vol.get("target")
            else:
                parts = str(vol).split(":")
                if len(parts) < 2:
                    continue
                host, target = parts[0], parts[1]
            if not host or not target:
                continue
            label = next(
                (lbl for prefix, lbl in BIND_TARGET_LABELS if target.rstrip("/").startswith(prefix)), None
            )
            if label is None:
                continue
            host_path = Path(host).expanduser()
            if not host_path.is_absolute():
                host_path = project_dir / host_path
            host_path = Path(os.path.normpath(str(host_path)))
            if host_path.exists():
                result.append((host_path, label))
    return result


ANON_VOLUME_RE = re.compile(r"^[0-9a-f]{64}$")


def all_volume_sizes():
    """{volume_name: bytes} for every local volume (from `docker system df -v`)."""
    out = _docker("system", "df", "-v").decode()
    lines = out.splitlines()
    sizes = {}
    start = None
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("local volumes space usage"):
            start = i + 1
            break
    if start is None:
        return sizes
    for line in lines[start:]:
        if not line.strip():
            if sizes:
                break
            continue
        if line.strip().lower().startswith("volumes space usage") or line.strip().startswith("VOLUME NAME"):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 3:
            continue
        name, size_text = parts[0], parts[-1]
        size = parse_bytes(size_text)
        if size is not None:
            sizes[name] = size
    return sizes


def project_volume_sizes(slug):
    """{volume_name: bytes} for named volumes belonging to a compose project."""
    return {name: size for name, size in all_volume_sizes().items() if name.startswith(f"{slug}_")}


def anonymous_volume_orphans():
    """Anonymous volumes no container uses anymore (hash-named, auto-created)."""
    out = _docker("volume", "ls", "-f", "dangling=true", "--format", "{{json .}}").decode()
    sizes = all_volume_sizes()
    orphans = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(row.get("Name", ""))
        if not ANON_VOLUME_RE.match(name):
            continue  # named volumes may belong to unregistered projects - leave them
        orphans.append({"name": name, "size_bytes": sizes.get(name)})
    return orphans


def filestore_dirs(entry):
    """[dirname] inside /var/lib/odoo/filestore of the web container."""
    web = entry["services"]["web"]
    proc = compose.exec_service(entry["path"], web, "ls", "-1", testing.FILESTORE_DIR, check=False)
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.decode().splitlines() if line.strip()]


def filestore_size(entry, db):
    web = entry["services"]["web"]
    proc = compose.exec_service(entry["path"], web, "du", "-sb", f"{testing.FILESTORE_DIR}/{db}", check=False)
    if proc.returncode != 0:
        return None
    first = proc.stdout.decode().split()
    return int(first[0]) if first and first[0].isdigit() else None


# ------------------------------------------------------------------- gc plan


@dataclass
class GCItem:
    kind: str  # images | builder | db | filestore | dir | deep-volumes
    description: str
    target: str
    project: str | None = None
    bytes_free: int | None = None
    entry: dict = field(default_factory=dict, repr=False)
    dbname: str | None = None

    @property
    def size_label(self):
        return fmt_bytes(self.bytes_free) if self.bytes_free is not None else "-"


def build_gc_plan(projects=None, keep_backups=3, keep_logs=20):
    from . import registry as registry_mod

    projects = projects if projects is not None else registry_mod.get_projects()
    items: list[GCItem] = []

    dangling = dangling_images()
    if dangling:
        items.append(
            GCItem(
                kind="images",
                description=f"remove {len(dangling)} dangling image layer(s)",
                target="(docker image prune)",
                bytes_free=sum(d["size_bytes"] or 0 for d in dangling),
            )
        )

    build = system_df().get("build cache") or {}
    if build.get("reclaim_bytes"):
        items.append(
            GCItem(
                kind="builder",
                description="prune docker build cache (next builds will be slower)",
                target="(docker builder prune)",
                bytes_free=build["reclaim_bytes"],
            )
        )

    try:
        orphans = anonymous_volume_orphans()
    except SpaceError:
        orphans = []
    for vol in orphans:
        items.append(
            GCItem(
                kind="volume",
                description="orphan anonymous volume (no container uses it)",
                target=vol["name"],
                bytes_free=vol["size_bytes"],
            )
        )

    for slug, entry in sorted(projects.items()):
        path = Path(entry["path"])

        for doomed in plan_backup_prunes(path / "backups" / "odooctl", keep_backups):
            items.append(
                GCItem(
                    kind="dir",
                    description="old backup snapshot",
                    target=str(doomed),
                    project=slug,
                    bytes_free=du_bytes(doomed),
                )
            )
        for doomed in plan_log_prunes(path / "backups" / "test_logs", keep_logs):
            items.append(
                GCItem(
                    kind="dir",
                    description="old test log",
                    target=str(doomed),
                    project=slug,
                    bytes_free=du_bytes(doomed),
                )
            )

        state_db = state_web = None
        try:
            state_db, _ = compose.service_state(entry["path"], entry["services"]["db"])
            state_web, _ = compose.service_state(entry["path"], entry["services"]["web"])
        except compose.DockerError:
            continue  # compose file gone/broken: host-side items were still handled above
        if state_db != "running":
            continue
        dbs = compose.databases(entry["path"], entry.get("db_user", "odoo")) or []
        test_dbs = [d for d in dbs if d.startswith("test_")]
        for db in test_dbs:
            items.append(
                GCItem(
                    kind="db",
                    description=f"drop throwaway test database '{db}' (+filestore)",
                    target=db,
                    project=slug,
                    entry=entry,
                    dbname=db,
                )
            )

        if state_web != "running":
            continue
        stores = filestore_dirs(entry)
        if stores is None:
            continue
        live = set(dbs) | {"__system__"}
        for store in stores:
            if store.startswith("test_") or store in live:
                continue
            items.append(
                GCItem(
                    kind="filestore",
                    description="orphan filestore (no matching database)",
                    target=store,
                    project=slug,
                    bytes_free=filestore_size(entry, store),
                    entry=entry,
                    dbname=store,
                )
            )

    return items


_KIND_ORDER = {
    "rmi": 0,  # removing images first frees MORE cache to become cleanable
    "images": 1,
    "db": 2,
    "filestore": 3,
    "dir": 4,
    "volume": 5,
    "deep-volumes": 6,
    "builder": 9,  # always last: sweep up cache orphaned by the steps above
}


def execute(items, echo=lambda msg: None):
    freed = 0
    for item in sorted(items, key=lambda i: (_KIND_ORDER.get(i.kind, 5), i.project or "")):
        if item.kind == "images":
            _docker("image", "prune", "-f")
        elif item.kind == "builder":
            _docker("builder", "prune", "-f")
        elif item.kind == "db":
            testing.cleanup_db_artifacts(item.entry["path"], item.entry, item.dbname)
        elif item.kind == "filestore":
            testing.drop_filestore(item.entry["path"], item.entry, item.dbname)
        elif item.kind == "dir":
            shutil.rmtree(item.target, ignore_errors=True)
        elif item.kind == "rmi":
            _docker("rmi", "-f", item.target)
        elif item.kind == "volume":
            _docker("volume", "rm", item.target)
        elif item.kind == "deep-volumes":
            compose.run(item.entry["path"], "down", "-v")
        else:
            raise SpaceError(f"Unknown gc action '{item.kind}'")
        if item.bytes_free:
            freed += item.bytes_free
        echo(f"  done: {item.description} [{item.project or 'global'}]")
    return freed
