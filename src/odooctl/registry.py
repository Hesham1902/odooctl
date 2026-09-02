import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .compose import find_compose_file


def _config_dir():
    return Path(os.environ.get("ODOOCTL_HOME", str(Path.home() / ".config" / "odooctl")))


def _config_file():
    return _config_dir() / "config.json"


def default_roots():
    """Static roots persisted on first run. The current directory is scanned, never saved."""
    return [
        str(Path.home() / "Developer" / "Work"),
        str(Path.home() / "odoo-projects"),
    ]


def normalize_root(root):
    return str(Path(root).expanduser().resolve())


def ephemeral_roots(persisted):
    """Roots scanned on top of the saved ones without being persisted: the current directory,
    unless it is home, the filesystem root, or already inside a saved root."""
    cwd = Path.cwd().resolve()
    if cwd in (Path.home().resolve(), Path(cwd.anchor)):
        return []
    for root in persisted:
        try:
            if cwd.is_relative_to(Path(root).expanduser().resolve()):
                return []
        except OSError:
            continue
    return [str(cwd)]


# Counted in path parts relative to a root, filename included: a project may sit at most
# SCAN_DEPTH - 1 folders below the root.
SCAN_DEPTH = 4
MAX_PROJECT_DEPTH = SCAN_DEPTH - 1
SKIP_DIR_NAMES = ("node_modules",)


def load_config():
    if _config_file().exists():
        return json.loads(_config_file().read_text())
    return {"roots": default_roots(), "projects": {}}


def save_config(cfg):
    _config_dir().mkdir(parents=True, exist_ok=True)
    _config_file().write_text(json.dumps(cfg, indent=2))


def _slug_for(web_svc_name, container_name, project_dir):
    if container_name:
        base = container_name.lower()
        for suffix in ("_web", "_odoo", "-web"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        base = base.replace(" ", "-")
        if base:
            return base
    return project_dir.name.lower().replace(" ", "-")


def _extract_ports(service_def):
    ports = {}
    for item in service_def.get("ports", []) or []:
        mapping = str(item)
        if ":" not in mapping:
            continue
        host_part, _, container_part = mapping.rpartition(":")
        try:
            host = int(host_part.split(":")[-1])
            container = int(container_part)
        except ValueError:
            continue
        if container == 8069:
            ports["http"] = host
        elif container == 8072:
            ports["longpolling"] = host
        elif container == 5432:
            ports["postgres"] = host
        elif container == 8888:
            ports["debugpy"] = host
    return ports


def _env_map(svc):
    env = svc.get("environment") or {}
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out = {}
    for item in env:
        item = str(item)
        if "=" in item:
            key, _, value = item.partition("=")
            out[key] = value
    return out


def _identity_blob(svc):
    """The parts of a service that say what it *is*: image, build, container name, command."""
    parts = [svc.get("image"), svc.get("container_name"), svc.get("command"), svc.get("build")]
    return json.dumps(parts, default=str).lower()


def _is_db(svc):
    blob = _identity_blob(svc)
    return "postgres" in blob or "postgis" in blob


def _looks_web(name, svc):
    blob = _identity_blob(svc)
    volumes = json.dumps(svc.get("volumes") or [], default=str).lower()
    if "odoo" in name.lower() or "odoo" in blob or "odoo" in volumes:
        return True
    if "/mnt/extra-addons" in volumes:
        return True
    return "debugpy" in json.dumps(svc, default=str).lower()


NO_WEB_REASON = "no Odoo web service (no service name/image/build/volumes mention 'odoo')"
NO_DB_REASON = "no Postgres service (no image/build mentions postgres/postgis)"


def _pick_services(services):
    """Return (web, db, reason). reason is None when both were found."""
    web = db = None
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        if _is_db(svc):
            db = db or name
        elif _looks_web(name, svc):
            web = web or name
    if web is None:
        return None, None, NO_WEB_REASON
    if db is None:
        return None, None, NO_DB_REASON
    return web, db, None


def _build_entry(compose_path, services, web, db):
    web_svc = services[web]
    db_svc = services[db]
    env = _env_map(db_svc)
    entry = {
        "compose_file": str(compose_path),
        "path": str(compose_path.parent),
        "services": {"web": web, "db": db},
        "container_names": {
            "web": web_svc.get("container_name"),
            "db": db_svc.get("container_name"),
        },
        "ports": _extract_ports(web_svc) | {f"pg_{k}": v for k, v in _extract_ports(db_svc).items()},
        "db_user": env.get("POSTGRES_USER", "odoo"),
        "images": {
            "web": web_svc.get("image"),
            "db": db_svc.get("image"),
        },
    }
    slug = _slug_for(web, entry["container_names"]["web"], compose_path.parent)
    addons_dir = compose_path.parent / "custom_addons"
    if addons_dir.is_dir():
        entry["custom_addons"] = str(addons_dir)
    return slug, entry


def _load_services(compose_path):
    data = yaml.safe_load(compose_path.read_text()) or {}
    services = data.get("services") if isinstance(data, dict) else None
    return services or {}


def parse_compose(compose_path: Path):
    """(slug, entry) for an Odoo compose file, None when it does not look like one."""
    services = _load_services(compose_path)
    web, db, reason = _pick_services(services)
    if reason:
        return None
    return _build_entry(compose_path, services, web, db)


@dataclass
class ScanReport:
    """What a scan looked at, so the CLI can explain an empty result."""

    roots: dict = field(default_factory=dict)  # root -> compose files seen; None = root missing
    rejected: list = field(default_factory=list)  # (compose file path, reason)
    ephemeral: set = field(default_factory=set)  # roots scanned but not persisted (cwd)

    @property
    def compose_files_seen(self):
        return sum(count for count in self.roots.values() if count)

    @property
    def missing_roots(self):
        return [root for root, count in self.roots.items() if count is None]


def _depth_reason(root, depth):
    return f"too deep: {depth} folders below {root} (max {MAX_PROJECT_DEPTH}); add a closer root"


def _scan_root(root, found, report, origins, seen_files):
    root_path = Path(root)
    report.roots[root] = 0
    visited = set()
    for dirpath, dirnames, _filenames in os.walk(root_path, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in visited:
            dirnames[:] = []
            continue
        visited.add(real)
        current = Path(dirpath)
        depth = len(current.relative_to(root_path).parts)

        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIR_NAMES)
        if depth >= MAX_PROJECT_DEPTH:
            for child in dirnames:
                child_compose = find_compose_file(current / child)
                if child_compose is not None and os.path.realpath(child_compose) not in seen_files:
                    report.rejected.append((str(child_compose), _depth_reason(root, depth + 1)))
            dirnames[:] = []

        compose_path = find_compose_file(current)
        if compose_path is None:
            continue
        real_file = os.path.realpath(compose_path)
        if real_file in seen_files:  # already handled via another (overlapping) root
            continue
        seen_files.add(real_file)
        report.roots[root] += 1
        try:
            services = _load_services(compose_path)
        except (OSError, yaml.YAMLError) as exc:
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            report.rejected.append((str(compose_path), f"YAML parse error: {first_line}"))
            continue
        web, db, reason = _pick_services(services)
        if reason:
            report.rejected.append((str(compose_path), reason))
            continue
        slug, entry = _build_entry(compose_path, services, web, db)
        if slug in found:
            report.rejected.append(
                (str(compose_path), f"slug '{slug}' already registered from {origins[slug]}")
            )
            continue
        found[slug] = entry
        origins[slug] = entry["path"]


def scan(roots, ephemeral=()):
    """Walk roots for Odoo compose projects. Returns (found, ScanReport)."""
    found = {}
    origins = {}
    report = ScanReport()
    seen_files = set()
    ordered = {}  # display order: saved roots first, then ephemeral ones
    for root, is_ephemeral in [(r, False) for r in roots] + [(r, True) for r in ephemeral]:
        root = normalize_root(root)
        if root in ordered:
            continue
        ordered[root] = is_ephemeral
        report.roots[root] = None
        if is_ephemeral:
            report.ephemeral.add(root)
    # Scan the deepest roots first so a parent root does not claim (or flag as too deep) a
    # compose file that a nested root covers properly.
    for root in sorted(ordered, key=lambda r: -len(Path(r).parts)):
        if Path(root).is_dir():
            _scan_root(root, found, report, origins, seen_files)
    return found, report


def discover(roots):
    return scan(roots)[0]


def refresh_registry(roots=None, forget=()):
    """Merge new roots, drop forgotten ones, rescan. Returns (config, ScanReport)."""
    cfg = load_config()
    current = {normalize_root(r) for r in (cfg.get("roots") or default_roots())}
    current |= {normalize_root(r) for r in (roots or ())}
    current -= {normalize_root(r) for r in forget}
    cfg["roots"] = sorted(current)
    cfg["projects"], report = scan(cfg["roots"], ephemeral_roots(cfg["roots"]))
    save_config(cfg)
    return cfg, report


def get_projects():
    cfg = load_config()
    projects = cfg.get("projects") or {}
    if not projects:
        roots = cfg.get("roots") or default_roots()
        cfg["projects"], _ = scan(roots, ephemeral_roots(roots))
        save_config(cfg)
        projects = cfg["projects"]
    return projects


def detect_version(entry):
    text = ""
    try:
        text = Path(entry["compose_file"]).read_text()
    except (OSError, KeyError):
        return None
    m = re.search(r"odoo-(\d+)\.\d", text)
    if m:
        return f"{m.group(1)}.0"
    dockerfile = Path(entry.get("path", "")) / "odoo.Dockerfile"
    if dockerfile.is_file():
        m = re.search(r"odoo[:\s@-]+v?(\d{2})", dockerfile.read_text())
        if m:
            return f"{m.group(1)}.0"
    return None


def register(slug, entry):
    cfg = load_config()
    cfg["projects"][slug] = entry
    save_config(cfg)


def unregister(slug):
    cfg = load_config()
    cfg["projects"].pop(slug, None)
    save_config(cfg)


def normalize_version(value):
    m = re.match(r"(\d+)", str(value))
    return f"{m.group(1)}.0" if m else None


def save_pull_settings(slug, data):
    """Store pull connection details outside project entries (discover wipes those)."""
    cfg = load_config()
    cfg.setdefault("pull", {})[slug] = {k: v for k, v in data.items() if v}
    save_config(cfg)


def load_pull_settings(slug):
    return (load_config().get("pull") or {}).get(slug) or {}


def resolve(name_or_prefix, projects=None):
    projects = projects or get_projects()
    if name_or_prefix in projects:
        return name_or_prefix, projects[name_or_prefix]
    matches = [k for k in projects if k.startswith(name_or_prefix)]
    if len(matches) == 1:
        return matches[0], projects[matches[0]]
    known = ", ".join(sorted(projects)) or "(none - run `odooctl discover`)"
    raise KeyError(f"Unknown project '{name_or_prefix}'. Registered: {known}")
