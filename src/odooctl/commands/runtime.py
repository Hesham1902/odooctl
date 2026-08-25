import datetime
import subprocess
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

from .. import compose, logparse, registry, watcher
from .common import entry, measure, need_docker, wait_http
from .root import main


def _inspect_project(item):
    slug, project = item
    try:
        rows = compose.ps(project["path"])
        db_running = any(
            row.get("Service") == project["services"]["db"] and (row.get("State") or "").lower() == "running"
            for row in rows
        )
        databases = (
            compose.databases(project["path"], project.get("db_user", "odoo"), check_running=False)
            if db_running
            else None
        )
        return slug, project, rows, databases, None
    except compose.DockerError as exc:
        return slug, project, [], None, exc


@main.command(section="Runtime")
@click.argument("project", required=False)
def status(project):
    """Show container state + databases (all projects by default)."""
    need_docker()
    if project:
        slug, project_entry = entry(project)
        items = {slug: project_entry}
    else:
        items = registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet.\nHint: run `odooctl discover` to find projects.")

    sorted_items = sorted(items.items())
    with measure("project status"):
        if len(sorted_items) == 1:
            results = [_inspect_project(sorted_items[0])]
        else:
            workers = min(8, len(sorted_items))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                results = list(executor.map(_inspect_project, sorted_items))

    for slug, project_entry, rows, databases, error in results:
        path = project_entry["path"]
        ports = project_entry.get("ports", {})
        http = ports.get("http", "-")
        postgres = ports.get("postgres") or ports.get("pg_postgres", "-")
        click.secho(f"{slug}  ({path})  http:{http}  pg:{postgres}", bold=True)
        if error:
            click.secho(f"  error: {error}", fg="red")
            click.echo()
            continue
        if not rows:
            click.echo("  containers: down")
        else:
            for row in rows:
                name = row.get("Name") or row.get("Service")
                state = row.get("State", "?")
                color = "green" if state == "running" else "red"
                click.secho(f"  {row.get('Service', '?'):<5} {name}: {state}", fg=color)
        if databases:
            click.echo(f"  databases: {', '.join(databases)}")
        click.echo()


@main.command(section="Runtime")
@click.argument("project")
@click.option("--build", is_flag=True, help="Rebuild images before starting.")
@click.option("--no-wait", is_flag=True, help="Do not wait for Odoo to answer HTTP.")
def up(project, build, no_wait):
    """Start a project's containers."""
    need_docker()
    slug, project_entry = entry(project)
    args = ["up", "-d"] + (["--build"] if build else [])
    click.echo(f"[{slug}] starting...")
    compose.run(project_entry["path"], *args)
    port = project_entry.get("ports", {}).get("http")
    if no_wait or not port:
        click.echo(f"[{slug}] up.")
        return
    click.echo(f"[{slug}] waiting for http://localhost:{port} ...")
    if wait_http(port):
        click.secho(f"[{slug}] ready -> http://localhost:{port}", fg="green")
    else:
        click.secho(
            f"[{slug}] started but not answering yet; check `odooctl logs {slug}`.",
            fg="yellow",
        )


@main.command(section="Runtime")
@click.argument("project")
def down(project):
    """Stop a project's containers."""
    need_docker()
    slug, project_entry = entry(project)
    compose.run(project_entry["path"], "down")
    click.echo(f"[{slug}] down.")


@main.command(section="Runtime")
@click.argument("project")
@click.option("--service", "-s", default=None, help="Service to restart (default: web).")
def restart(project, service):
    """Restart one service (default: web)."""
    need_docker()
    slug, project_entry = entry(project)
    service = service or project_entry["services"]["web"]
    compose.run(project_entry["path"], "restart", service)
    click.echo(f"[{slug}] restarted {service}.")


@main.command(section="Runtime")
@click.argument("project")
@click.option("--follow", "-f", is_flag=True, help="Stream logs.")
@click.option(
    "--tail",
    "-t",
    default=None,
    type=int,
    help="Lines to scan (default: 100, or 2000 with --errors).",
)
@click.option("--service", "-s", default=None, help="Service (default: web).")
@click.option(
    "--errors",
    "-e",
    is_flag=True,
    help="Only show ERROR/CRITICAL lines including their tracebacks.",
)
def logs(project, follow, tail, service, errors):
    """Show or stream logs."""
    need_docker()
    slug, project_entry = entry(project)
    service = service or project_entry["services"]["web"]
    if tail is None:
        tail = 2000 if errors else 100
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    args.append(service)

    if not errors:
        if follow:
            compose.live_output(project_entry["path"], *args)
        else:
            proc = compose.run(project_entry["path"], *args)
            click.echo(proc.stdout.decode(errors="replace"))
        return

    error_filter = logparse.ErrorFilter()

    def emit(line):
        text = line.decode(errors="replace") if isinstance(line, bytes) else line
        if error_filter.feed(text) and text.strip():
            click.echo(text.rstrip("\n"))

    if follow:
        cmd = compose._base(project_entry["path"]) + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        try:
            for raw in proc.stdout:
                emit(raw)
        except KeyboardInterrupt:
            pass
        finally:
            proc.terminate()
    else:
        proc = compose.run(project_entry["path"], *args)
        blocks = [
            line
            for line in proc.stdout.decode(errors="replace").splitlines(keepends=True)
            if error_filter.feed(line)
        ]
        if not blocks:
            click.secho(f"[{slug}] no errors in the last {tail} lines.", fg="green")
            return
        shown = 0
        for line in blocks:
            if line.strip():
                click.echo(line.rstrip("\n"))
                shown += 1
        click.secho(f"\n[{slug}] {shown} error/critical line(s) in the last {tail}.", fg="yellow")


@main.command("open", aliases=("url",), section="Runtime")
@click.argument("project")
def open_project(project):
    """Open the project in your browser."""
    slug, project_entry = entry(project)
    port = project_entry.get("ports", {}).get("http")
    if not port:
        raise click.ClickException(f"No HTTP port detected for {slug}.")
    target = f"http://localhost:{port}"
    click.echo(target)
    webbrowser.open(target)


@main.command(section="Runtime")
@click.argument("project")
@click.option("--ext", default="py", show_default=True, help="Comma-separated file extensions to watch.")
@click.option("--interval", type=float, default=0.5, show_default=True, help="Poll seconds.")
@click.option(
    "--debounce",
    type=float,
    default=0.8,
    show_default=True,
    help="Settle time after a change before restarting.",
)
def dev(project, ext, interval, debounce):
    """Watch custom_addons and restart web when source files change."""
    need_docker()
    slug, project_entry = entry(project)
    addons_dir = project_entry.get("custom_addons")
    if not addons_dir or not Path(addons_dir).is_dir():
        raise click.ClickException(f"{slug} has no custom_addons folder on disk.")
    if not compose.web_running(project_entry["path"], project_entry):
        raise click.ClickException(
            f"[{slug}] web container is not running.\nHint: start it with `odooctl up {slug}`."
        )
    extensions = {value.strip().lstrip(".").lower() for value in ext.split(",") if value.strip()}
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    click.secho(f"[{slug}] dev mode at {stamp} - Ctrl-C to stop.", fg="cyan", bold=True)
    try:
        watcher.watch(
            addons_dir,
            exts=extensions,
            interval=interval,
            debounce=debounce,
            echo=lambda message: click.echo(f"[{slug}] {message}"),
            restart=lambda: compose.run(project_entry["path"], "restart", project_entry["services"]["web"]),
        )
    except KeyboardInterrupt:
        click.echo(f"[{slug}] dev mode stopped.")
