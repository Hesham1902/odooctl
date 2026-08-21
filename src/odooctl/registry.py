import json
import os
import re
from pathlib import Path

import yaml


def _config_dir():
    return Path(os.environ.get("ODOOCTL_HOME", str(Path.home() / ".config" / "odooctl")))


def _config_file():
    return _config_dir() / "config.json"


def default_roots():
    return [
        str(Path.home() / "Developer" / "Work"),
        str(Path.home() / "odoo-projects"),
        str(Path.cwd()),
    ]


SCAN_DEPTH = 4


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


def parse_compose(compose_path: Path):
    data = yaml.safe_load(compose_path.read_text()) or {}
    services = data.get("services") or {}
    web = db = None
    for name, svc in services.items():
        blob = json.dumps(svc, default=str).lower()
        is_db = (
            "postgres" in blob
            or "postgis" in blob
            or str(svc.get("build", {}).get("dockerfile", "")).lower().find("postgres") >= 0
        )
        looks_web = any(
            marker in blob
            for marker in ("odoo", "/mnt/extra-addons", "debugpy")
        )
        if is_db and svc.get("depends_on") is None:
            db = name if db is None else db
        elif looks_web and "postgres" not in blob:
            web = name if web is None else web
    if web is None:
        for name, svc in services.items():
            if str(svc.get("build", {}).get("dockerfile", "")).lower().find("odoo") >= 0:
                web = name
                break
    if db is None:
        for name, svc in services.items():
            if "postgres" in json.dumps(svc, default=str).lower():
                db = name
                break
    if web is None or db is None:
        return None
    web_svc = services[web]
    db_svc = services[db]
    env = {e.split("=", 1)[0]: e.split("=", 1)[-1] for e in (db_svc.get("environment") or []) if "=" in str(e)}
    entry = {
        "compose_file": str(compose_path),
        "path": str(compose_path.parent),
        "services": {"web": web, "db": db},
        "container_names": {
            "web": web_svc.get("container_name"),
            "db": db_svc.get("container_name"),
        },
        "ports": _extract_ports(web_svc) | {
            f"pg_{k}": v for k, v in _extract_ports(db_svc).items()
        },
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


def discover(roots):
    found = {}
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("docker-compose.yml")):
            if len(path.relative_to(root).parts) > SCAN_DEPTH:
                continue
            if any(part.startswith((".", "node_modules")) for part in path.parts):
                continue
            try:
                parsed = parse_compose(path)
            except Exception:
                continue
            if parsed:
                slug, entry = parsed
                found[slug] = entry
    return found


def refresh_registry(roots=None):
    cfg = load_config()
    if roots:
        cfg["roots"] = sorted(set(cfg.get("roots", [])) | {str(Path(r).expanduser()) for r in roots})
    cfg["projects"] = discover(cfg.get("roots") or default_roots())
    save_config(cfg)
    return cfg


def get_projects():
    cfg = load_config()
    projects = cfg.get("projects") or {}
    if not projects:
        roots = cfg.get("roots") or default_roots()
        cfg["projects"] = discover(roots)
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


def resolve(name_or_prefix, projects=None):
    projects = projects or get_projects()
    if name_or_prefix in projects:
        return name_or_prefix, projects[name_or_prefix]
    matches = [k for k in projects if k.startswith(name_or_prefix)]
    if len(matches) == 1:
        return matches[0], projects[matches[0]]
    known = ", ".join(sorted(projects)) or "(none - run `odooctl discover`)"
    raise KeyError(f"Unknown project '{name_or_prefix}'. Registered: {known}")
