import copy
import re
import shutil
import socket
from pathlib import Path

import yaml

from . import registry

COPY_ITEMS = ["docker-compose.yml", "odoo.Dockerfile", "postgres.Dockerfile", "config"]
HOME_PREFIX = str(Path.home())


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or None


def _is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _allocator(taken):
    used = set(taken)

    def alloc(preferred):
        for port in [preferred, *range(preferred + 1, preferred + 200)]:
            if port not in used and _is_free(port):
                used.add(port)
                return port
        raise RuntimeError(f"No free port found near {preferred}")

    return alloc


def _all_taken_ports(projects):
    taken = set()
    for entry in projects.values():
        for port in (entry.get("ports") or {}).values():
            try:
                taken.add(int(port))
            except (TypeError, ValueError):
                continue
    return taken


def rewrite_compose(data, slug, version, alloc):
    data = copy.deepcopy(data)
    data["name"] = slug
    services = data["services"]
    web = db = None
    for name, svc in services.items():
        blob = str(svc).lower()
        if db is None and "postgres" in blob:
            db = name
        elif web is None:
            web = name
    for name, role in ((web, "web"), (db, "db")):
        svc = services[name]
        svc.pop("image", None)
        svc["container_name"] = f"{slug}_{role}"
        new_ports = []
        for mapping in (svc.get("ports") or []):
            host, _, container = str(mapping).rpartition(":")
            try:
                preferred = int(host.split(":")[-1])
            except ValueError:
                continue
            new_ports.append(f"{alloc(preferred)}:{container}")
        if new_ports:
            svc["ports"] = new_ports
        volumes = []
        for vol in (svc.get("volumes") or []):
            if isinstance(vol, str) and "/_odoo_addons/" in vol:
                left, _, right = vol.partition(":")
                left = re.sub(r"^/(home|Users)/[^/]+", HOME_PREFIX, left)
                if version:
                    left = re.sub(r"odoo-\d+\.\d+", f"odoo-{version}", left)
                vol = f"{left}:{right}"
            volumes.append(vol)
        if volumes:
            svc["volumes"] = volumes
    return data, web, db


def ports_of(data, web_name, db_name):
    def host_port(svc_name, container_port):
        for mapping in (data["services"].get(svc_name) or {}).get("ports") or []:
            host, _, container = str(mapping).rpartition(":")
            if container == container_port:
                return int(host.split(":")[-1])
        return None

    ports = {}
    for key, container in (("http", "8069"), ("longpolling", "8072"), ("debugpy", "8888")):
        value = host_port(web_name, container)
        if value:
            ports[key] = value
    pg = host_port(db_name, "5432")
    if pg:
        ports["pg_postgres"] = pg
    return ports


def pick_template(projects, version, template_slug):
    from . import compose

    if template_slug:
        if template_slug not in projects:
            raise RuntimeError(f"Unknown template '{template_slug}'. Registered: {', '.join(sorted(projects))}")
        return template_slug, projects[template_slug]
    if not version:
        raise RuntimeError("Need --version or --template.")
    candidates = sorted(
        (s, e) for s, e in projects.items() if registry.detect_version(e) == version
    )
    if not candidates:
        known = {s: registry.detect_version(e) for s, e in projects.items()}
        raise RuntimeError(f"No registered project on Odoo {version}. Known: {known}")
    for slug, entry in candidates:
        try:
            web_img = compose.find_built_image(entry, "web")
            db_img = compose.find_built_image(entry, "db")
        except Exception:
            continue
        if web_img and db_img:
            return slug, entry
    return candidates[0]


def init_project(name, parent_dir, version=None, template_slug=None, dry_run=False):
    projects = registry.get_projects()
    if not projects:
        raise RuntimeError("No registered projects to use as template. Run `odooctl discover`.")

    slug = slugify(name)
    if not slug:
        raise RuntimeError(f"Cannot derive a folder name from '{name}'.")
    tmpl_slug, tmpl_entry = pick_template(projects, version, template_slug)
    tmpl_version = registry.detect_version(tmpl_entry)

    target = Path(parent_dir).expanduser() / slug
    if target.exists():
        raise RuntimeError(f"{target} already exists.")

    data = yaml.safe_load(Path(tmpl_entry["compose_file"]).read_text())
    alloc = _allocator(_all_taken_ports(projects))
    new_data, web_name, db_name = rewrite_compose(data, slug, tmpl_version, alloc)
    ports = ports_of(new_data, web_name, db_name)

    plan = {
        "slug": slug,
        "path": str(target),
        "template": tmpl_slug,
        "version": tmpl_version,
        "ports": ports,
        "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
        "copy": COPY_ITEMS + ["custom_addons/ (empty)"],
    }
    if dry_run:
        return plan, None

    target.mkdir(parents=True)
    for item in COPY_ITEMS:
        src = Path(tmpl_entry["path"]) / item
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, target / item)
        else:
            shutil.copy2(src, target / item)
    (target / "custom_addons").mkdir()

    (target / "docker-compose.yml").write_text(
        yaml.safe_dump(new_data, sort_keys=False, default_flow_style=False)
    )

    entry = {
        "compose_file": str(target / "docker-compose.yml"),
        "path": str(target),
        "services": {"web": web_name, "db": db_name},
        "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
        "ports": ports,
        "db_user": tmpl_entry.get("db_user", "odoo"),
        "custom_addons": str(target / "custom_addons"),
        "images": {"web": f"{slug}-web", "db": f"{slug}-db"},
    }
    registry.register(slug, entry)
    return plan, entry
