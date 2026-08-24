import datetime
import json
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import click

from . import admin, compose, dbdiff, logparse, provision, registry, space, testing, vcs, watcher
from . import deps as deps_mod
from . import pull as pull_mod
from . import restore as restore_mod
from . import sanitize as sanitize_mod
from .manifest import list_addons

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
ODOO_CONF = "/etc/odoo/odoo.conf"
GC_KEEP_BACKUPS = 3
GC_KEEP_LOGS = 20


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(package_name="odooctl", prog_name="odooctl")
def main():
    """One CLI for all your local Odoo docker environments."""


def _entry(name):
    try:
        return registry.resolve(name)
    except KeyError as exc:
        raise click.ClickException(str(exc))


def _need_docker():
    if not compose.daemon_available():
        raise click.ClickException(
            "Docker daemon not reachable. Start Docker Desktop and try again."
        )


def _wait_http(port, timeout=120):
    url = f"http://localhost:{port}/web/login"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(2)
    return False


def _print_project_line(slug, entry):
    ports = entry.get("ports", {})
    http = ports.get("http", "-")
    pg = ports.get("postgres") or ports.get("pg_postgres", "-")
    click.echo(f"{slug:<14} {entry['path']:<50} http:{http:<6} pg:{pg}")


@main.command()
@click.option("--root", multiple=True, help="Extra directory to scan for projects.")
def discover(root):
    """(Re)scan your work folders for Odoo docker projects."""
    cfg = registry.refresh_registry(roots=root or None)
    projects = cfg["projects"]
    if not projects:
        raise click.ClickException(
            "No Odoo projects found under the scan roots.\n"
            f"Current roots: {', '.join(cfg.get('roots') or registry.default_roots())}\n"
            "Add yours with: odooctl discover --root /path/to/projects"
        )
    click.echo(f"Found {len(projects)} project(s):")
    for slug, entry in sorted(projects.items()):
        _print_project_line(slug, entry)


@main.command("projects")
def projects_cmd():
    """List registered projects."""
    items = registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet - run `odooctl discover`.")
    for slug, entry in sorted(items.items()):
        _print_project_line(slug, entry)


@main.command()
@click.argument("project", required=False)
def status(project):
    """Show container state + databases (all projects by default)."""
    _need_docker()
    items = {project: _entry(project)[1]} if project else registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet - run `odooctl discover`.")
    for slug, entry in sorted(items.items()):
        path = entry["path"]
        ports = entry.get("ports", {})
        http = ports.get("http", "-")
        pg = ports.get("postgres") or ports.get("pg_postgres", "-")
        click.secho(f"{slug}  ({path})  http:{http}  pg:{pg}", bold=True)
        try:
            rows = compose.ps(path)
            if not rows:
                click.echo("  containers: down")
            else:
                for row in rows:
                    name = row.get("Name") or row.get("Service")
                    state = row.get("State", "?")
                    color = "green" if state == "running" else "red"
                    click.secho(f"  {row.get('Service', '?'):<5} {name}: {state}", fg=color)
            dbs = compose.databases(path, entry.get("db_user", "odoo"))
            if dbs:
                click.echo(f"  databases: {', '.join(dbs)}")
        except compose.DockerError as exc:
            click.secho(f"  error: {exc}", fg="red")
        click.echo()


@main.command()
@click.argument("project")
@click.option("--build", is_flag=True, help="Rebuild images before starting.")
@click.option("--no-wait", is_flag=True, help="Do not wait for Odoo to answer HTTP.")
def up(project, build, no_wait):
    """Start a project's containers."""
    _need_docker()
    slug, entry = _entry(project)
    args = ["up", "-d"] + (["--build"] if build else [])
    click.echo(f"[{slug}] starting...")
    compose.run(entry["path"], *args)
    port = entry.get("ports", {}).get("http")
    if no_wait or not port:
        click.echo(f"[{slug}] up.")
        return
    click.echo(f"[{slug}] waiting for http://localhost:{port} ...")
    if _wait_http(port):
        click.secho(f"[{slug}] ready -> http://localhost:{port}", fg="green")
    else:
        click.secho(f"[{slug}] started but not answering yet; check `odooctl logs {slug}`.", fg="yellow")


@main.command()
@click.argument("project")
def down(project):
    """Stop a project's containers."""
    _need_docker()
    slug, entry = _entry(project)
    compose.run(entry["path"], "down")
    click.echo(f"[{slug}] down.")


@main.command()
@click.argument("project")
@click.option("--service", "-s", default=None, help="Service to restart (default: web).")
def restart(project, service):
    """Restart one service (default: web)."""
    _need_docker()
    slug, entry = _entry(project)
    svc = service or entry["services"]["web"]
    compose.run(entry["path"], "restart", svc)
    click.echo(f"[{slug}] restarted {svc}.")


@main.command()
@click.argument("project")
@click.option("--follow", "-f", is_flag=True, help="Stream logs.")
@click.option("--tail", "-t", default=None, type=int,
              help="Lines to scan (default: 100, or 2000 with --errors).")
@click.option("--service", "-s", default=None, help="Service (default: web).")
@click.option("--errors", "-e", is_flag=True,
              help="Only show ERROR/CRITICAL lines including their tracebacks.")
def logs(project, follow, tail, service, errors):
    """Show / stream logs."""
    _need_docker()
    slug, entry = _entry(project)
    svc = service or entry["services"]["web"]
    if tail is None:
        tail = 2000 if errors else 100
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    args.append(svc)

    if not errors:
        if follow:
            compose.live_output(entry["path"], *args)
        else:
            proc = compose.run(entry["path"], *args)
            click.echo(proc.stdout.decode(errors="replace"))
        return

    flt = logparse.ErrorFilter()

    def emit(line):
        text = line.decode(errors="replace") if isinstance(line, bytes) else line
        if flt.feed(text) and text.strip():
            click.echo(text.rstrip("\n"))

    if follow:
        cmd = compose._base(entry["path"]) + args
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        try:
            for raw in proc.stdout:
                emit(raw)
        except KeyboardInterrupt:
            pass
        finally:
            proc.terminate()
    else:
        proc = compose.run(entry["path"], *args)
        blocks = [ln for ln in proc.stdout.decode(errors="replace").splitlines(keepends=True)
                  if flt.feed(ln)]
        if not blocks:
            click.secho(f"[{slug}] no errors in the last {tail} lines.", fg="green")
            return
        shown = 0
        for ln in blocks:
            if ln.strip():
                click.echo(ln.rstrip("\n"))
                shown += 1
        click.secho(f"\n[{slug}] {shown} error/critical line(s) in the last {tail}.", fg="yellow")


@main.command()
@click.argument("project")
def url(project):
    """Open the project in your browser."""
    slug, entry = _entry(project)
    port = entry.get("ports", {}).get("http")
    if not port:
        raise click.ClickException(f"No http port detected for {slug}.")
    target = f"http://localhost:{port}"
    click.echo(target)
    webbrowser.open(target)


@main.command()
@click.argument("project")
@click.option("--db", "-d", default=None, help="Database (default: postgres maintenance db).")
def psql(project, db):
    """Interactive psql session on the db container."""
    _need_docker()
    slug, entry = _entry(project)
    compose.run(
        entry["path"],
        "exec",
        entry["services"]["db"],
        "psql",
        "-U",
        entry.get("db_user", "odoo"),
        "-d",
        db or "postgres",
        capture=False,
        check=False,
    )


@main.command()
@click.argument("project")
@click.option("--db", "-d", default=None, help="Database to open the shell on.")
def shell(project, db):
    """Interactive `odoo shell` session (ORM REPL) for one database."""
    _need_docker()
    slug, entry = _entry(project)
    if not compose.web_running(entry["path"], entry):
        raise click.ClickException(
            f"[{slug}] web container is not running. Start it first: odooctl up {slug}"
        )
    if not db:
        found = compose.databases(entry["path"], entry.get("db_user", "odoo")) or []
        if len(found) == 1:
            db = found[0]
        elif not found:
            raise click.ClickException("No databases found. Is the project running? (`odooctl up`)")
        else:
            raise click.ClickException(
                f"Multiple databases: {', '.join(found)}\nPick one with --db (-d)."
            )
    click.echo(f"[{slug}] odoo shell on '{db}' (exit with exit() or Ctrl-D)")
    compose.run(
        entry["path"],
        "exec",
        entry["services"]["web"],
        "odoo", "shell", "-c", ODOO_CONF, "-d", db, "--no-http",
        capture=False,
        check=False,
    )


@main.command()
@click.argument("project")
@click.option("--from", "from_", required=False, default=None,
              help="SSH target, e.g. ssh://acme@acme.odoo.sh or user@server (key auth). "
                   "Omit when saved with --save previously.")
@click.option("--path", default=None,
              help="Remote path to a specific backup (default: newest in ~/backup.daily etc.).")
@click.option("--db", "-d", default=None, help="Database name for the restore.")
@click.option("--no-reset-admin", is_flag=True, help="Keep restored credentials as-is.")
@click.option("--keep-download", is_flag=True, help="Keep the downloaded zip after restoring.")
@click.option("--key", type=click.Path(exists=True), default=None,
              help="SSH private key file (e.g. ~/.ssh/id_ed25519_acme).")
@click.option("--save", is_flag=True,
              help="Remember --from/--path/--key/--db for this project; afterwards just: odooctl pull PROJECT.")
@click.option("--with-filestore", is_flag=True,
              help="Also download attachments (can be large). Default: skip - dev copies rarely need them.")
@click.option("--yes", "-y", is_flag=True, help="Skip the overwrite confirmation (for scripts/CI).")
def pull(project, from_, path, db, no_reset_admin, keep_download, key, save, with_filestore, yes):
    """Pull the latest backup over SSH and restore it into the project."""
    _need_docker()
    slug, entry = _entry(project)
    saved = registry.load_pull_settings(slug)
    from_ = from_ or saved.get("from")
    if not from_:
        raise click.ClickException(
            "Pass --from ssh://user@host the first time "
            "(add --save to remember it; then plain 'odooctl pull <project>' works)."
        )
    path = path or saved.get("path")
    key = key or saved.get("key")
    db = db or saved.get("db")
    try:
        target, port = pull_mod.parse_target(from_)
    except pull_mod.PullError as exc:
        raise click.ClickException(str(exc))
    if save:
        registry.save_pull_settings(slug, {
            "from": from_,
            "path": path,
            "key": str(Path(key).expanduser()) if key else None,
            "db": db,
        })
        click.echo(f"[{slug}] saved pull settings (next time: odooctl pull {slug}).")

    dbname = db or f"{slug}_pulled"
    if not db:
        click.secho(f"(no -d given; restoring as '{dbname}')", fg="yellow")

    state, _ = compose.service_state(entry["path"], entry["services"]["db"])
    if state != "running":
        raise click.ClickException(
            f"[{slug}] db container is not running. Start it first: odooctl up {slug}")
    existing = compose.databases(entry["path"], entry.get("db_user", "odoo")) or []
    if dbname in existing and not yes:
        click.confirm(f"Database '{dbname}' already exists. DROP it and restore over it?", abort=True)

    click.echo(f"[{slug}] looking for the latest backup on {target}...")
    try:
        remote = pull_mod.find_remote_backup(target, port=port, path=path, key=key)
        fs_note = " (filestore available)" if remote.get("mirror") else " (dump only)"
        click.echo(f"[{slug}] found {remote['sql_gz']}{fs_note}")
        if not with_filestore:
            click.echo(f"[{slug}] skipping filestore (--with-filestore to include attachments)")
        local = pull_mod.download(target, port, remote,
                                  Path(entry["path"]) / "backups" / "pulled",
                                  key=key, with_filestore=with_filestore)
    except pull_mod.PullError as exc:
        raise click.ClickException(str(exc))
    size_mb = sum(p.stat().st_size for p in local.rglob("*") if p.is_file()) / 1e6
    click.echo(f"[{slug}] downloaded {local.name}/ ({size_mb:.1f} MB)")
    if local.is_dir():
        gzs = sorted(local.glob("*.sql.gz"))
        source_db = restore_mod._dump_create_target(gzs[0]) if gzs else None
        if source_db:
            click.echo(f"[{slug}] source database in dump: '{source_db}' -> will become '{dbname}'")

    was_running = compose.web_running(entry["path"], entry)
    if was_running:
        click.echo(f"[{slug}] stopping web for the restore...")
        compose.run(entry["path"], "stop", entry["services"]["web"])
    click.echo(f"[{slug}] restoring into '{dbname}'...")
    try:
        info = restore_mod.restore(entry["path"], entry, local, dbname)
    except (ValueError, compose.DockerError) as exc:
        if was_running:
            compose.run(entry["path"], "start", entry["services"]["web"])
        raise click.ClickException(f"restore failed: {exc}")
    if with_filestore and not info["filestore"]:
        click.secho("[!] no filestore found remotely - attachments missing", fg="yellow")
    for ext in info.get("skipped_extensions") or []:
        click.secho(f"[!] postgres extension '{ext}' not available locally - skipped "
                    f"(install pgvector etc. in your db image if you ever need it)", fg="yellow")

    if not no_reset_admin:
        try:
            res = admin.reset_admin(entry["path"], entry, dbname)
            click.secho(
                f"[{slug}] login ready: admin / admin  (user #{res['id']}, was '{res['old_login']}')",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    if was_running:
        click.echo(f"[{slug}] starting web back...")
        compose.run(entry["path"], "start", entry["services"]["web"])

    if keep_download:
        click.echo(f"[{slug}] bundle kept at {local}")
    else:
        shutil.rmtree(local, ignore_errors=True)
        click.echo(f"[{slug}] cleaned up download.")

    port_http = entry.get("ports", {}).get("http")
    if port_http:
        click.secho(f"[{slug}] done -> http://localhost:{port_http}", fg="green")


@main.command()
@click.argument("project")
@click.option("--ext", default="py", show_default=True,
              help="Comma-separated file extensions to watch.")
@click.option("--interval", type=float, default=0.5, show_default=True, help="Poll seconds.")
@click.option("--debounce", type=float, default=0.8, show_default=True,
              help="Settle time after a change before restarting.")
def dev(project, ext, interval, debounce):
    """Watch custom_addons for .py changes and restart web automatically.

    Host-side polling - reliable on macOS bind mounts where inotify inside the
    container never fires.
    """
    _need_docker()
    slug, entry = _entry(project)
    addons_dir = entry.get("custom_addons")
    if not addons_dir or not Path(addons_dir).is_dir():
        raise click.ClickException(f"{slug} has no custom_addons folder on disk.")
    if not compose.web_running(entry["path"], entry):
        raise click.ClickException(
            f"[{slug}] web container is not running. Start it first: odooctl up {slug}"
        )
    exts = {e.strip().lstrip(".").lower() for e in ext.split(",") if e.strip()}
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    click.secho(f"[{slug}] dev mode at {stamp} - Ctrl-C to stop.", fg="cyan", bold=True)
    try:
        watcher.watch(
            addons_dir, exts=exts, interval=interval, debounce=debounce,
            echo=lambda msg: click.echo(f"[{slug}] {msg}"),
            restart=lambda: compose.run(entry["path"], "restart", entry["services"]["web"]),
        )
    except KeyboardInterrupt:
        click.echo(f"[{slug}] dev mode stopped.")


@main.command()
@click.argument("project")
@click.argument("module")
def deps(project, module):
    """Show an addon's dependency tree, reverse deps and cycles (from manifests on disk)."""
    slug, entry = _entry(project)
    addons_dir = entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException(f"{slug} has no custom_addons folder registered.")
    addons = list_addons(addons_dir)
    if module not in addons:
        raise click.ClickException(
            f"Module '{module}' not found in {addons_dir}.\n"
            f"On disk: {', '.join(sorted(addons)) or '(none)'}"
        )
    graph = deps_mod.build_graph(addons)

    def render(mod, prefix="", seen=None):
        seen = seen if seen is not None else {module}
        children = [d for d in graph.get(mod, []) if d != mod]
        for i, dep in enumerate(children):
            last = i == len(children) - 1
            branch = "`-- " if last else "|-- "
            if dep in addons:
                version = addons[dep].get("version", "")
                label = f"{dep} ({version})" if version else dep
            else:
                label = f"{dep} [external]"
            repeat = ""
            if dep in seen and dep in addons:
                repeat = "  (already shown)"
            click.secho(f"{prefix}{branch}{label}{repeat}",
                        fg="cyan" if dep in addons else "bright_black")
            if dep in addons and dep not in seen:
                seen.add(dep)
                render(dep, prefix + ("    " if last else "|   "), seen)

    mf = addons[module]
    version = mf.get("version", "")
    header = f"{module}" + (f"  {version}" if version else "")
    deps_count = len(graph.get(module, []))
    click.secho(header, bold=True)
    render(module)
    transitive = sorted(deps_mod.transitive_deps(graph, module))
    internal = [t for t in transitive if t in addons]
    external = [t for t in transitive if t not in addons]
    click.echo()
    click.echo(f"direct: {deps_count} | transitive: "
               f"{len(internal)} custom, {len(external)} external")

    rev = deps_mod.dependents(graph)
    users = sorted(rev.get(module, ()))
    if users:
        click.secho(f"required by: {', '.join(users)}", fg="yellow")

    cycle = deps_mod.find_cycle(graph, module)
    if cycle:
        click.secho(f"CYCLE DETECTED: {' -> '.join(cycle)}", fg="red", bold=True)


@main.command()
@click.argument("project")
@click.argument("db_a")
@click.argument("db_b")
@click.option("--grep", "-g", default=None, help="Filter module names.")
def diff(project, db_a, db_b, grep):
    """Compare module state/version between two databases of one project."""
    _need_docker()
    slug, entry = _entry(project)

    def fetch(db):
        proc = compose.exec_service(
            entry["path"], "db", "psql", "-U", entry.get("db_user", "odoo"),
            "-d", db, "-At", "-F", "|",
            "-c", "SELECT name, state, latest_version FROM ir_module_module ORDER BY name",
        )
        return dbdiff.parse_module_states(proc.stdout.decode(errors="replace"))

    states_a, states_b = fetch(db_a), fetch(db_b)
    if grep:
        states_a = {k: v for k, v in states_a.items() if grep.lower() in k.lower()}
        states_b = {k: v for k, v in states_b.items() if grep.lower() in k.lower()}

    result = dbdiff.compare(states_a, states_b)
    total = len(result["changed"]) + len(result["only_a"]) + len(result["only_b"])
    if not total:
        click.secho(f"[{slug}] '{db_a}' and '{db_b}' are in sync.", fg="green")
        return

    width = max([len(db_a), len(db_b), 24])
    click.echo(f"{'MODULE':<40} {db_a:<{width}}  {db_b}")
    for name, (a, b) in result["changed"].items():
        left, right = f"{a[0]} {a[1]}", f"{b[0]} {b[1]}"
        click.echo(f"{name:<40} {left:<{width}}  {right}")
    for name in result["only_a"]:
        a = states_a[name]
        click.echo(f"{name:<40} {a[0]} {a[1]:<{width - len(a[0]) - 1}}  -")
    for name in result["only_b"]:
        b = states_b[name]
        click.echo(f"{name:<40} {'-':<{width}}  {b[0]} {b[1]}")

    click.echo()
    if result["only_a"]:
        click.secho(f"only in {db_a} ({len(result['only_a'])}): "
                    f"{', '.join(result['only_a'][:10])}"
                    + (f", +{len(result['only_a']) - 10} more" if len(result["only_a"]) > 10 else ""),
                    fg="cyan")
    if result["only_b"]:
        click.secho(f"only in {db_b} ({len(result['only_b'])}): "
                    f"{', '.join(result['only_b'][:10])}"
                    + (f", +{len(result['only_b']) - 10} more" if len(result["only_b"]) > 10 else ""),
                    fg="cyan")
    click.secho(f"{total} module(s) differ.", fg="yellow")


@main.command()
@click.argument("project")
@click.argument("module", required=False)
@click.option("--db", "-d", default=None, help="Database to read install state from.")
@click.option("--grep", "-g", default=None, help="Filter module names.")
def addons(project, module, db, grep):
    """List custom addons (from disk) joined with install state (from DB)."""
    _need_docker()
    slug, entry = _entry(project)
    addons_dir = entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException(f"{slug} has no custom_addons folder registered.")

    if not db:
        found = compose.databases(entry["path"], entry.get("db_user", "odoo")) or []
        if len(found) == 1:
            db = found[0]
        elif not found:
            raise click.ClickException("No databases found. Is the project running? (`odooctl up`)")
        else:
            raise click.ClickException(
                f"Multiple databases: {', '.join(found)}\nPick one with --db (-d)."
            )

    local = list_addons(addons_dir)
    if grep:
        local = {k: v for k, v in local.items() if grep.lower() in k.lower()}
    states = {}
    try:
        names = sorted(local)
        if names:
            quoted = ",".join("'" + n.replace("'", "''") + "'" for n in names)
            sql = (
                "SELECT name, state, latest_version FROM ir_module_module "
                f"WHERE name IN ({quoted}) ORDER BY name"
            )
            proc = compose.exec_service(
                entry["path"], "db", "psql", "-U", entry.get("db_user", "odoo"),
                "-d", db, "-At", "-F", "|", "-c", sql,
            )
            for line in proc.stdout.decode().splitlines():
                parts = line.split("|")
                if parts and parts[0]:
                    states[parts[0]] = (parts[1] if len(parts) > 1 else "-", parts[2] if len(parts) > 2 else "-")
    except compose.DockerError as exc:
        click.secho(f"(DB state unavailable: {exc})", fg="yellow")

    if module:
        manifest = local.get(module) or {}
        click.echo(json.dumps(manifest, indent=2))
        if module in states:
            click.echo(f"state: {states[module][0]}  version: {states[module][1]}")
        return

    click.echo(f"{'MODULE':<42} {'VERSION':<12} {'STATE':<12} DEPENDS")
    click.secho(f"(db: {db})", fg="cyan")
    for name, mf in sorted(local.items()):
        version = str(mf.get("version", "-"))
        state, dbver = states.get(name, ("-", "-"))
        deps = ", ".join(mf.get("depends", []) or [])
        shown = dbver or version
        click.echo(f"{name:<42} {shown:<12} {state:<12} {deps[:60]}")


def _resolve_changed(entry, since):
    addons_dir = entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException("Project has no custom_addons folder registered.")
    try:
        mods = vcs.changed_modules(entry["path"], addons_dir, ref=since)
    except vcs.GitError as exc:
        raise click.ClickException(f"git failed: {exc}")
    return addons_dir, mods


@main.command()
@click.argument("project")
@click.argument("module", nargs=-1, required=False)
@click.option("--db", "-d", required=True, help="Database to upgrade the module(s) in.")
@click.option("--changed", is_flag=True,
              help="Upgrade every custom addon changed vs git HEAD (or --since REF).")
@click.option("--since", default=None, help="Git ref to diff against (with --changed).")
@click.option("--keep-stopped", is_flag=True, help="Leave web stopped afterwards.")
def upgrade(project, module, db, changed, since, keep_stopped):
    """Upgrade one or more addons (-u M1,M2 --stop-after-init), restarting web around it."""
    _need_docker()
    slug, entry = _entry(project)
    if changed and module:
        raise click.ClickException("Pass either MODULE(s) or --changed, not both.")
    if changed:
        _, modules = _resolve_changed(entry, since)
        if not modules:
            raise click.ClickException(
                f"No changed custom addons detected under {entry.get('custom_addons')}."
            )
    elif module:
        modules = list(module)
    else:
        raise click.ClickException("Pass a module name or --changed.")

    click.secho(f"[{slug}] upgrading: {', '.join(modules)}", fg="cyan")
    web = entry["services"]["web"]
    was_running = compose.web_running(entry["path"], entry)
    if was_running:
        click.echo(f"[{slug}] stopping web...")
        compose.run(entry["path"], "stop", web)
    cmd = ["odoo", "-c", ODOO_CONF, "-d", db, "-u", ",".join(modules), "--stop-after-init"]
    rc = compose.live_output(entry["path"], "run", "--rm", web, *cmd)
    if was_running and not keep_stopped:
        click.echo(f"[{slug}] starting web back...")
        compose.run(entry["path"], "start", web)
    if rc != 0:
        raise click.ClickException(f"[{slug}] upgrade failed (rc={rc}).")
    click.secho(f"[{slug}] upgraded {', '.join(modules)} in {db}.", fg="green")


@main.command()
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to back up.")
@click.option("--keep", "-k", type=int, default=None,
              help="Keep only the newest N snapshots of this db (default: no pruning).")
def backup(project, db, keep):
    """Backup a database + its filestore to backups/odooctl/."""
    _need_docker()
    slug, entry = _entry(project)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(entry["path"]) / "backups" / "odooctl" / f"{db}_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)

    click.echo(f"[{slug}] dumping database '{db}'...")
    dump_path = dest / "db.dump"
    with open(dump_path, "wb") as fh:
        compose.exec_service(
            entry["path"], "db", "pg_dump", "-U", entry.get("db_user", "odoo"), "-Fc", db,
            capture=False, stdout_file=fh,
        )

    click.echo(f"[{slug}] archiving filestore...")
    fs_path = dest / "filestore.tar.gz"
    with open(fs_path, "wb") as fh:
        compose.exec_service(
            entry["path"], "web", "tar", "czf", "-", "-C", "/var/lib/odoo/filestore", db,
            capture=False, stdout_file=fh,
        )

    (dest / "meta.json").write_text(json.dumps({"database": db, "created": stamp}))
    size_mb = sum(p.stat().st_size for p in dest.iterdir()) / 1e6
    click.secho(f"[{slug}] backup saved to {dest} ({size_mb:.1f} MB)", fg="green")

    if keep is not None:
        doomed = space.plan_backup_prunes(dest.parent, keep)
        for old in doomed:
            shutil.rmtree(old, ignore_errors=True)
        if doomed:
            click.echo(f"[{slug}] pruned {len(doomed)} older snapshot(s) of '{db}' (kept {keep}).")

    click.echo(f"restore with: odooctl restore {slug} {dest}")


@main.command()
@click.argument("name")
@click.option("--version", "-v", default=None, help="Odoo version, e.g. 18 or 16.0 (or inferred from backup zip).")
@click.option("--template", "-t", default=None, help="Copy docker setup from this registered project.")
@click.option("--from", "from_", type=click.Path(exists=True), default=None,
              help="Backup to restore (.zip from Odoo.sh, .dump, or odooctl backup dir).")
@click.option("--db", "-d", default=None, help="Database name for the restore.")
@click.option("--parent-dir", "-p", type=click.Path(file_okay=False), default=None,
              help="Folder to create the project in (default: first scan root).")
@click.option("--no-build", is_flag=True, help="Start without building anything.")
@click.option("--build", is_flag=True, help="Force a fresh image build (default: reuse the template's image - instant).")
@click.option("--no-reset-admin", is_flag=True, help="Skip admin/admin reset after restore.")
@click.option("--dry-run", is_flag=True, help="Show the plan without creating anything.")
def init(name, version, template, from_, db, parent_dir, no_build, build, no_reset_admin, dry_run):
    """Bootstrap a brand-new local Odoo project from an existing one."""
    _need_docker()
    norm_version = registry.normalize_version(version) if version else None
    backup_path = Path(from_) if from_ else None
    if backup_path and backup_path.is_file() and not norm_version and not template:
        inferred = restore_mod.zip_server_version(backup_path)
        if inferred:
            click.echo(f"inferred Odoo {inferred} from backup manifest")
            norm_version = inferred

    if not parent_dir:
        existing_roots = [
            r for r in (registry.load_config().get("roots") or [])
            if Path(r).expanduser().is_dir()
        ]
        parent_dir = existing_roots[0] if existing_roots else str(Path.cwd())

    try:
        plan, entry = provision.init_project(
            name, parent_dir, version=norm_version, template_slug=template, dry_run=dry_run
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc))

    click.secho(f"new project   : {plan['slug']}  ->  {plan['path']}", bold=True)
    click.echo(f"template      : {plan['template']} (Odoo {plan['version'] or '?'})")
    click.echo(f"containers    : {plan['container_names']['web']}, {plan['container_names']['db']}")
    click.echo(f"ports         : {plan['ports']}")
    click.echo(f"copying       : {', '.join(plan['copy'])}")
    if dry_run:
        click.echo("(dry run - nothing created)")
        return

    slug = plan["slug"]

    tmpl_slug, _ = registry.resolve(plan["template"])
    tmpl_entry = registry.get_projects()[tmpl_slug]
    reuse = None
    if not build and not no_build:
        web_img = compose.find_built_image(tmpl_entry, "web")
        db_img = compose.find_built_image(tmpl_entry, "db")
        if web_img and db_img:
            for src, dst in ((web_img, f"{slug}-web"), (db_img, f"{slug}-db")):
                subprocess.run(["docker", "tag", src, dst], check=True)
            reuse = f"{web_img} -> {slug}-web"

    if no_build:
        compose.run(entry["path"], "up", "-d", "--no-build")
    elif reuse:
        click.echo(f"[{slug}] reusing template image ({reuse}) - skipping build")
        compose.run(entry["path"], "up", "-d", "--no-build")
    else:
        click.echo(f"\n[{slug}] building images (first time only, ~5-10 min)...")
        compose.run(entry["path"], "up", "-d", "--build")

    if backup_path:
        dbname = restore_mod.target_name(backup_path, restore_mod.detect_format(backup_path), db)
        if not dbname:
            dbname = f"{slug}_db"
            click.secho(f"(no name in backup; using '{dbname}')", fg="yellow")
        click.echo(f"[{slug}] restoring {backup_path.name} into '{dbname}'...")
        try:
            info = restore_mod.restore(entry["path"], entry, backup_path, dbname)
        except (ValueError, compose.DockerError) as exc:
            raise click.ClickException(f"restore failed: {exc}")
        if not info["filestore"]:
            click.secho("[!] no filestore in backup - attachments missing", fg="yellow")
        if not no_reset_admin:
            click.echo(f"[{slug}] resetting admin credentials...")
            try:
                res = admin.reset_admin(entry["path"], entry, dbname)
                click.secho(
                    f"[{slug}] login ready: admin / admin  (user #{res['id']}, was '{res['old_login']}')",
                    fg="green",
                )
            except (compose.DockerError, RuntimeError) as exc:
                click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    port = entry.get("ports", {}).get("http")
    click.echo(f"\n[{slug}] waiting for Odoo to boot (first boot can take a minute)...")
    if port and _wait_http(port, timeout=300):
        click.secho(f"[{slug}] ready -> http://localhost:{port}", fg="green", bold=True)
    else:
        click.secho(f"[{slug}] still booting; check `odooctl logs {slug} -f`.", fg="yellow")
    click.echo(f"next: odooctl logs {slug} -f   |   odooctl url {slug}")


@main.command("reset-admin")
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database name.")
@click.option("--login", "-l", default="admin", show_default=True)
@click.option("--password", "-p", default="admin", show_default=True)
@click.option("--user-id", "-u", type=int, default=None, help="Force a specific res_users id.")
def reset_admin(project, db, login, password, user_id):
    """Reset the main internal user's login/password (e.g. after Odoo.sh restore)."""
    _need_docker()
    slug, entry = _entry(project)
    try:
        info = admin.reset_admin(entry["path"], entry, db, login, password, user_id)
    except (compose.DockerError, RuntimeError) as exc:
        raise click.ClickException(str(exc))
    click.secho(
        f"[{slug}] user #{info['id']} ({info['name']}) was '{info['old_login']}' -> now '{login}' / '{password}'",
        fg="green",
    )


@main.command()
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to sanitize.")
@click.option("--names", is_flag=True, help="Also replace partner names with 'Partner #id'.")
@click.option("--keep-crons", is_flag=True, help="Leave scheduled actions enabled.")
@click.option("--keep-mail", is_flag=True, help="Skip mail queue purge / server disable.")
def sanitize(project, db, names, keep_crons, keep_mail):
    """Make a restored (prod) DB safe locally: pause crons, kill mail, scrub PII."""
    _need_docker()
    slug, entry = _entry(project)
    click.echo(f"[{slug}] sanitizing '{db}'...")
    try:
        counts = sanitize_mod.sanitize(entry["path"], entry, db,
                                       with_names=names,
                                       keep_crons=keep_crons,
                                       keep_mail=keep_mail)
    except (compose.DockerError, RuntimeError) as exc:
        raise click.ClickException(str(exc))
    for key, label in sanitize_mod.LABELS:
        if counts.get(key):
            click.secho(f"[{slug}] {counts[key]:>6}  {label}", fg="green")
    click.secho(f"[{slug}] '{db}' is now safe to work on.", fg="green")


@main.command()
@click.argument("project")
@click.argument("backup_path", type=click.Path(exists=True))
@click.option("--name", "-n", default=None, help="Restore under a different DB name.")
@click.option("--reset-admin/--no-reset-admin", "reset_admin_flag", default=True,
              help="Reset main user to admin/admin afterwards (default: yes).")
@click.option("--sanitize/--no-sanitize", "sanitize_flag", default=False,
              help="Make the restored DB safe locally (pause crons, purge mail, scrub PII).")
def restore(project, backup_path, name, reset_admin_flag, sanitize_flag):
    """Restore a backup (odooctl dir, .dump, or Odoo.sh .zip)."""
    _need_docker()
    slug, entry = _entry(project)
    src = Path(backup_path)
    target = restore_mod.target_name(src, restore_mod.detect_format(src), name)
    if not target:
        raise click.ClickException("Could not determine a database name - pass --name.")

    existing = compose.databases(entry["path"], entry.get("db_user", "odoo")) or []
    if target in existing:
        click.confirm(f"Database '{target}' already exists. DROP it and restore over it?", abort=True)

    was_running = compose.web_running(entry["path"], entry)
    if was_running:
        click.echo(f"[{slug}] stopping web for the restore...")
        compose.run(entry["path"], "stop", entry["services"]["web"])
    click.echo(f"[{slug}] restoring into '{target}'...")
    try:
        info = restore_mod.restore(entry["path"], entry, src, target)
    except (ValueError, compose.DockerError) as exc:
        if was_running:
            compose.run(entry["path"], "start", entry["services"]["web"])
        raise click.ClickException(str(exc))
    if not info["filestore"]:
        click.secho("[!] No filestore found in backup - attachments will be missing.", fg="yellow")
    for ext in info.get("skipped_extensions") or []:
        click.secho(f"[!] postgres extension '{ext}' not available locally - skipped", fg="yellow")

    if reset_admin_flag:
        try:
            res = admin.reset_admin(entry["path"], entry, target)
            click.secho(
                f"[{slug}] admin reset: login 'admin' / 'admin' (user #{res['id']}, was '{res['old_login']}')",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    if was_running:
        click.echo(f"[{slug}] starting web back...")
        compose.run(entry["path"], "start", entry["services"]["web"])

    if sanitize_flag:
        try:
            counts = sanitize_mod.sanitize(entry["path"], entry, target)
            for key, label in sanitize_mod.LABELS:
                if counts.get(key):
                    click.secho(f"[{slug}] {counts[key]:>6}  {label}", fg="green")
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] sanitize failed: {exc}", fg="yellow")

    click.secho(f"[{slug}] restored into '{target}'.", fg="green")


@main.command()
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to wipe.")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def reset(project, db, yes):
    """Drop a database and recreate it empty."""
    _need_docker()
    slug, entry = _entry(project)
    if not yes:
        click.confirm(f"Drop database '{db}' on {slug}? This cannot be undone.", abort=True)
    testing.cleanup_db_artifacts(entry["path"], entry, db)
    compose.exec_service(
        entry["path"], "db", "createdb", "-U", entry.get("db_user", "odoo"), db,
    )
    click.secho(f"[{slug}] '{db}' recreated empty (old filestore removed).", fg="green")


@main.command()
@click.argument("project")
@click.argument("module", required=False)
@click.option("--all", "all_", is_flag=True, help="Test every custom addon (sequential, own throwaway DB each).")
@click.option("--changed", is_flag=True,
              help="Test every custom addon changed vs git HEAD (or --since REF).")
@click.option("--since", default=None, help="Git ref to diff against (with --changed).")
@click.option("-x", "--stop-on-fail", is_flag=True, help="With --all/--changed: stop at the first failing module.")
@click.option("--db", "-d", default=None, help="Test DB name (default: test_<module>).")
@click.option("--keep-db", is_flag=True, help="Keep the throwaway test database after the run.")
@click.option("--test-tags", "-t", default=None, help="Override --test-tags (default: /<module>).")
@click.option("--timeout", type=int, default=None, help="Seconds before giving up.")
def test(project, module, all_, changed, since, stop_on_fail, db, keep_db, test_tags, timeout):
    """Run addon tests in a disposable database (one module, --all, or --changed)."""
    _need_docker()
    slug, entry = _entry(project)
    addons_dir = entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException(f"{slug} has no custom_addons folder registered.")

    selected = [flag for flag, on in (("--all", all_), ("--changed", changed)) if on]
    if len(selected) > 1 or (selected and module):
        raise click.ClickException("Pass only one of: MODULE, --all, --changed.")
    if not module and not selected:
        raise click.ClickException("Pass a module name, --all or --changed (see odooctl test --help).")

    multi = False
    if all_:
        manifests = list_addons(addons_dir)
        modules = [m for m, mf in sorted(manifests.items())
                   if mf.get("installable", True)]
        skipped = sorted(set(manifests) - set(modules))
        if not modules:
            raise click.ClickException(f"No installable addons found in {addons_dir}")
        click.echo(f"[{slug}] testing {len(modules)} module(s)"
                   + (f" (skipping not-installable: {', '.join(skipped)})" if skipped else ""))
        multi = True
    elif changed:
        _, modules = _resolve_changed(entry, since)
        if not modules:
            raise click.ClickException(
                f"No changed custom addons detected under {addons_dir}."
            )
        click.echo(f"[{slug}] testing {len(modules)} changed module(s): {', '.join(modules)}")
        multi = True
    else:
        if not (Path(addons_dir) / module / "__manifest__.py").exists():
            raise click.ClickException(f"Module '{module}' not found in {addons_dir}")
        modules = [module]

    results = []  # (name, TestResult)
    for idx, mod in enumerate(modules, 1):
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in mod)[:40]
        dbname = db if (db and not multi) else f"test_{safe}"
        label = f"({idx}/{len(modules)})" if multi else ""
        click.echo(f"[{slug}] {label} running tests for {mod} in throwaway DB '{dbname}'...")
        result = testing.run_tests(
            entry["path"], entry, mod, dbname,
            test_tags=test_tags, keep_db=keep_db, timeout=timeout,
        )
        results.append((mod, result))
        if stop_on_fail and not result.ok:
            click.secho(f"[{slug}] stopping on first failure.", fg="yellow")
            break

    if len(results) == 1 and not multi:
        mod, result = results[0]
        if result.ok:
            click.secho(f"[{slug}] PASS ({result.ran or '?'} tests)", fg="green")
        else:
            click.secho(f"[{slug}] FAIL", fg="red")
            for failure in result.failures[:20]:
                click.secho(f"   {failure}", fg="red")
            if result.raw_tail:
                click.echo("\n--- last log lines ---")
                click.echo(result.raw_tail)
    else:
        failed = [(m, r) for m, r in results if not r.ok]
        passed = [(m, r) for m, r in results if r.ok]
        click.echo(f"\n{'MODULE':<40} {'RESULT':<8} TESTS")
        for mod, result in passed:
            click.secho(f"{mod:<40} {'PASS':<8} {result.ran if result.ran else '-'}", fg="green")
        for mod, result in failed:
            click.secho(f"{mod:<40} {'FAIL':<8} {result.ran if result.ran else '-'}", fg="red")
        click.echo()
        click.secho(f"{len(passed)} passed, {len(failed)} failed"
                    + (f", {len(modules) - len(results)} not run" if stop_on_fail else ""),
                    bold=True)

    last = results[-1][1]
    if last.log_path:
        click.echo(f"log: {last.log_path}")
    sys.exit(0 if all(r.ok for _, r in results) else 1)


def _count(value):
    return value if value is not None else "?"


@main.command("df")
@click.argument("project", required=False)
def df_cmd(project):
    """Show docker disk usage per project + global reclaimable space."""
    _need_docker()
    items = {project: _entry(project)[1]} if project else registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet - run `odooctl discover`.")

    try:
        totals = space.system_df()
        dangling = space.dangling_images()
    except space.SpaceError as exc:
        raise click.ClickException(str(exc))
    images_t, volumes_t = totals.get("images", {}), totals.get("local volumes", {})
    build_t = totals.get("build cache", {})

    quick_win = (build_t.get("reclaim_bytes") or 0) + sum(d["size_bytes"] or 0 for d in dangling)

    # Resolve every project's image identity once, so sharing can be detected.
    usages = []  # (slug, role, ref, image_id, size)
    for slug, entry in sorted(items.items()):
        for role in ("web", "db"):
            try:
                ref = compose.find_built_image(entry, role)
                if not ref:
                    continue
                img_id, size = space.image_identity(ref)
            except (compose.DockerError, space.SpaceError):
                continue
            usages.append((slug, role, ref, img_id, size))
    shared_groups = space.group_shared_image_usage(usages)
    shared_savings = sum(
        g["size"] * (len(g["users"]) - 1)
        for g in shared_groups.values()
        if g["size"] and len(g["users"]) > 1
    )
    referenced_ids = {u[3] for u in usages if u[3]}

    try:
        all_images = space.list_images()
    except space.SpaceError:
        all_images = []
    # dangling images are untagged, so they never appear here
    untracked = space.filter_untracked(all_images, referenced_ids) if all_images else []
    untracked_bytes = sum(i["size_bytes"] or 0 for i in untracked)

    click.secho("GLOBAL DOCKER", bold=True)
    click.echo(
        f"  images      {space.fmt_bytes(images_t.get('size_bytes'))}"
        f"  ({_count(images_t.get('total'))} total / {_count(images_t.get('active'))} in use)"
    )
    if shared_savings:
        click.echo(f"              ({space.fmt_bytes(shared_savings)} already saved via layer sharing)")
    click.echo(
        f"  volumes     {space.fmt_bytes(volumes_t.get('size_bytes'))}  ({_count(volumes_t.get('total'))})"
    )
    click.echo(
        f"  build cache {space.fmt_bytes(build_t.get('size_bytes'))}"
        f"  (cleanable {space.fmt_bytes(build_t.get('reclaim_bytes'))})"
    )
    click.echo(f"  dangling    {len(dangling)} image(s), "
               f"{space.fmt_bytes(sum(d['size_bytes'] or 0 for d in dangling))}")
    if untracked:
        shown = sorted(untracked, key=lambda i: i["size_bytes"] or 0, reverse=True)[:4]
        extra = len(untracked) - len(shown)
        listing = "; ".join(f"{i['tag']} {space.fmt_bytes(i['size_bytes'])}" for i in shown)
        if extra > 0:
            listing += f"; +{extra} more"
        click.echo(f"  untracked   {len(untracked)} tagged image(s), {space.fmt_bytes(untracked_bytes)}")
        click.echo(f"              {listing}")
        click.secho("              (gc --stale-images removes these; bases may re-download on next build)",
                    fg="bright_black")
    click.secho(f"  quick win   ~{space.fmt_bytes(quick_win)} -> odooctl gc --apply\n", fg="cyan")

    grand_total = 0
    for slug, entry in sorted(items.items()):
        path = Path(entry["path"])
        version = registry.detect_version(entry) or "?"
        if not path.is_dir():
            click.secho(f"{slug}  ({path})", bold=True)
            click.echo("  folder missing - run `odooctl discover` to refresh the registry\n")
            continue
        click.secho(f"{slug}  ({path})  odoo {version}", bold=True)
        subtotal = 0

        for role in ("web", "db"):
            usage = next((u for u in usages if u[0] == slug and u[1] == role), None)
            label = f"{role} image"
            if usage is None:
                click.echo(f"  {label:<11} not built")
                continue
            _, _, ref, img_id, size = usage
            users = len(shared_groups[img_id]["users"]) if img_id in shared_groups else 1
            marker = f"  (shared ×{users})" if users > 1 else ""
            click.echo(f"  {label:<11} {ref}  {space.fmt_bytes(size)}{marker}")
            subtotal += size or 0

        try:
            vols = space.project_volume_sizes(slug)
        except space.SpaceError:
            vols = {}
        if vols:
            joined = " | ".join(f"{n} {space.fmt_bytes(b)}" for n, b in sorted(vols.items()))
            click.echo(f"  volumes     {joined}")
            subtotal += sum(vols.values())

        binds = []
        try:
            for host_path, label in space.bind_mounts(entry):
                binds.append((host_path, label, space.du_bytes(host_path)))
        except Exception:
            binds = []
        if binds:
            def display(p: Path):
                try:
                    rel = p.relative_to(path)
                    return str(rel)
                except ValueError:
                    return p.name
            joined = " | ".join(f"{lbl} {display(p)} {space.fmt_bytes(sz)}"
                                for p, lbl, sz in sorted(binds, key=lambda b: b[2], reverse=True))
            click.echo(f"  bind mounts {joined}")
            subtotal += sum(b[2] for b in binds)

        backups_dir = path / "backups" / "odooctl"
        groups = space.backup_groups(backups_dir)
        n_snaps = sum(len(v) for v in groups.values())
        backups_size = space.du_bytes(backups_dir)
        click.echo(
            f"  backups     {space.fmt_bytes(backups_size)}  ({n_snaps} snapshot(s))"
            f"   [gc keeps newest {GC_KEEP_BACKUPS}/db]"
        )
        logs_dir = path / "backups" / "test_logs"
        n_logs = len([p for p in logs_dir.glob("*") if p.is_file()]) if logs_dir.is_dir() else 0
        logs_size = space.du_bytes(logs_dir)
        click.echo(
            f"  test logs   {space.fmt_bytes(logs_size)}  ({n_logs} file(s))"
            f"   [gc keeps newest {GC_KEEP_LOGS}]"
        )
        subtotal += backups_size + logs_size
        grand_total += subtotal
        click.secho(f"  total       ~{space.fmt_bytes(subtotal)}", fg="bright_black")
        click.echo()

    if len(items) > 1:
        note = " (images counted per project; shared layers exist)" if shared_savings else ""
        click.secho(f"projects total ~{space.fmt_bytes(grand_total)}{note}", bold=True)


@main.command()
@click.argument("project", required=False)
@click.option("--apply", is_flag=True, help="Execute the cleanup (default: dry-run plan only).")
@click.option("--keep-backups", type=int, default=3, show_default=True,
              help="Backup snapshots to keep per database.")
@click.option("--keep-logs", type=int, default=20, show_default=True,
              help="Test log files to keep per project.")
@click.option("--stale-images", is_flag=True,
              help="Also remove tagged images no registered project uses (base images may re-download later).")
def gc(project, apply, keep_backups, keep_logs, stale_images):
    """Find and remove wasted space: dangling layers, build cache, test DBs, orphan filestores."""
    _need_docker()
    items = {project: _entry(project)[1]} if project else registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet - run `odooctl discover`.")

    try:
        plan = space.build_gc_plan(projects=items, keep_backups=keep_backups, keep_logs=keep_logs)
        if stale_images:
            referenced = set()
            for entry in items.values():
                for role in ("web", "db"):
                    try:
                        ref = compose.find_built_image(entry, role)
                        if ref:
                            img_id, _ = space.image_identity(ref)
                            if img_id:
                                referenced.add(img_id)
                    except (compose.DockerError, space.SpaceError):
                        continue
            for img in space.filter_untracked(space.list_images(), referenced):
                plan.append(space.GCItem(
                    kind="rmi",
                    description=f"remove unused image {img['tag']}",
                    target=img["id"],
                    bytes_free=img["size_bytes"],
                ))
    except (space.SpaceError, compose.DockerError) as exc:
        raise click.ClickException(str(exc))

    if not plan:
        click.secho("Nothing to clean - your environments are already tidy.", fg="green")
        return

    total = sum(i.bytes_free or 0 for i in plan)
    click.secho(f"Cleanup plan ({len(plan)} item(s), ~{space.fmt_bytes(total)} reclaimable):", bold=True)
    current = None
    for i in plan:
        if i.project != current:
            current = i.project
            click.secho(f"[{current or 'global'}]", fg="cyan")
        click.echo(f"  - {i.description:<55} {i.size_label:>10}  {i.target}")
    click.echo()

    if not apply:
        click.echo("Dry run only. Re-run with --apply to execute.")
        return

    freed = space.execute(plan, echo=lambda msg: click.echo(msg))
    click.secho(f"Reclaimed at least ~{space.fmt_bytes(freed)}. "
                f"On macOS the Docker Desktop disk shrinks lazily; restart Docker Desktop if needed.",
                fg="green")


@main.command("gc-deep")
@click.argument("project")
@click.option("--yes", is_flag=True, help="Skip confirmation.")
def gc_deep(project, yes):
    """NUCLEAR: wipe ALL named volumes of one project (every database!) and recreate them empty."""
    _need_docker()
    slug, entry = _entry(project)
    try:
        vols = space.project_volume_sizes(slug)
    except space.SpaceError as exc:
        raise click.ClickException(str(exc))
    total = sum(vols.values())
    names = ", ".join(sorted(vols)) or "(no named volumes)"
    click.secho(f"This deletes every database and filestore of '{slug}' ({names}, {space.fmt_bytes(total)}).",
                fg="red", bold=True)
    click.echo("Bind-mounted folders (addons, config, backups) are NOT touched.")
    if not yes:
        click.confirm("Wipe and recreate?", abort=True)
    item = space.GCItem(kind="deep-volumes", description="wipe project volumes", target=names, entry=entry)
    space.execute([item])
    click.secho(f"[{slug}] volumes wiped. Run `odooctl up {slug}` for a fresh environment.", fg="green")


@main.command()
@click.argument("project")
@click.option("--images", is_flag=True,
              help="Also remove the project's images (kept automatically if another project shares them).")
@click.option("--purge-folder", is_flag=True,
              help="Also DELETE the whole project folder from disk (source code, backups, data!).")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts.")
def remove(project, images, purge_folder, yes):
    """Delete a project: stop containers, drop volumes, unregister it.

    Bind mounts (databases, filestores) die with the folder only if you pass
    --purge-folder; by default your files are kept on disk.
    """
    slug, entry = _entry(project)
    path = Path(entry["path"])
    if not yes:
        click.secho(f"Removing '{slug}' ({path}) from odooctl.", bold=True)

    if path.is_dir():
        _need_docker()
        try:
            state_web, _ = compose.service_state(path, entry["services"]["web"])
            state_db, _ = compose.service_state(path, entry["services"]["db"])
            containers_exist = bool(state_web or state_db)
        except compose.DockerError:
            containers_exist = False
        if containers_exist:
            if not yes:
                click.echo(f"[{slug}] stopping and removing containers...")
            compose.run(path, "down", "--remove-orphans", "-v")
        elif not yes:
            click.echo(f"[{slug}] no containers found.")
    elif not yes:
        click.echo(f"[{slug}] folder already gone.")

    if images:
        for role in ("web", "db"):
            try:
                ref = compose.find_built_image(entry, role)
                if not ref:
                    continue
                img_id, size = space.image_identity(ref)
            except (compose.DockerError, space.SpaceError):
                continue
            if not img_id:
                continue
            sid = space.short_id(img_id)
            sharers = []
            for other_slug, other_entry in registry.get_projects().items():
                if other_slug == slug:
                    continue
                try:
                    oref = compose.find_built_image(other_entry, role)
                    oid, _ = space.image_identity(oref) if oref else (None, None)
                except (compose.DockerError, space.SpaceError):
                    continue
                if oid and space.short_id(oid) == sid:
                    sharers.append(other_slug)
            if sharers:
                click.echo(f"[{slug}] keeping {ref} ({space.fmt_bytes(size)}) - shared with {', '.join(sharers)}")
                continue
            subprocess.run(["docker", "rmi", ref], capture_output=True)
            click.echo(f"[{slug}] removed image {ref} ({space.fmt_bytes(size)})")

    if purge_folder and path.is_dir():
        if not yes:
            click.secho(f"About to DELETE {path} permanently (code, data/, backups).", fg="red", bold=True)
            click.confirm("This cannot be undone. Delete the folder?", abort=True)
        shutil.rmtree(path, ignore_errors=True)
        click.echo(f"[{slug}] folder deleted.")

    registry.unregister(slug)
    click.secho(f"[{slug}] removed from odooctl.", fg="green")


if __name__ == "__main__":
    main()
