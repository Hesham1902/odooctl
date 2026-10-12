import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

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


_DOCKER_APPS = (("OrbStack", "/Applications/OrbStack.app"), ("Docker Desktop", "/Applications/Docker.app"))
DOCKER_START_TIMEOUT = 90


def _installed_docker_app():
    if sys.platform != "darwin":
        return None
    for name, app in _DOCKER_APPS:
        if Path(app).is_dir():
            return name, app
    return None


def _offer_docker_start():
    """Interactive macOS only: start the installed Docker app and wait for the daemon."""
    found = _installed_docker_app()
    if not found or not sys.stdin.isatty():
        return False
    name, app = found
    if not click.confirm(f"Docker is not running. Start {name} now?", default=True):
        return False
    try:
        subprocess.run(["open", "-a", app], check=True, capture_output=True, timeout=30)
    except Exception as exc:
        click.secho(f"could not start {name}: {exc}", fg="yellow")
        return False
    click.echo(f"waiting for {name} to come up (up to {DOCKER_START_TIMEOUT}s)...", nl=False)
    deadline = time.time() + DOCKER_START_TIMEOUT
    while time.time() < deadline:
        if compose.daemon_available():
            click.echo(" ready.")
            return True
        click.echo(".", nl=False)
        time.sleep(2)
    click.echo("")
    return False


def need_docker():
    with measure("docker availability"):
        available = compose.daemon_available()
    if not available and _offer_docker_start():
        return
    if not available:
        raise click.ClickException(
            "Docker daemon not reachable. Start Docker Desktop or OrbStack and try again.\n"
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
