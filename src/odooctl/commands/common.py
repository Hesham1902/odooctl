import time
import urllib.request
from contextlib import contextmanager

import click

from .. import compose, registry

ODOO_CONF = "/etc/odoo/odoo.conf"


@contextmanager
def measure(label):
    """Record a timing only when the root command has ``--debug`` enabled."""
    ctx = click.get_current_context(silent=True)
    root = ctx.find_root() if ctx is not None else None
    enabled = bool(root and root.obj and root.obj.get("debug"))
    if not enabled:
        yield
        return

    started = time.perf_counter()
    try:
        yield
    finally:
        root.obj["timings"].append((label, time.perf_counter() - started))


def entry(name):
    try:
        with measure("registry resolve"):
            return registry.resolve(name)
    except KeyError as exc:
        raise click.ClickException(f"{exc}\nHint: run `odooctl projects` to list registered projects.")


def need_docker():
    with measure("docker availability"):
        available = compose.daemon_available()
    if not available:
        raise click.ClickException(
            "Docker daemon not reachable. Start Docker Desktop and try again.\n"
            "Hint: verify Docker with `docker info`."
        )


def pick_db(entry_data, db):
    if db:
        return db
    found = compose.databases(entry_data["path"], entry_data.get("db_user", "odoo")) or []
    if len(found) == 1:
        return found[0]
    if not found:
        raise click.ClickException(
            "No databases found. Is the project running?\nHint: start it with `odooctl up PROJECT`."
        )
    raise click.ClickException(f"Multiple databases: {', '.join(found)}\nHint: pick one with --db (-d).")


def wait_http(port, timeout=120):
    url = f"http://localhost:{port}/web/login"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


def print_project_line(slug, entry_data):
    ports = entry_data.get("ports", {})
    http = ports.get("http", "-")
    postgres = ports.get("postgres") or ports.get("pg_postgres", "-")
    click.echo(f"{slug:<14} {entry_data['path']:<50} http:{http:<6} pg:{postgres}")
