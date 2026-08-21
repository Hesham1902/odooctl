import datetime
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import click

from . import admin, compose, provision, registry, testing
from . import restore as restore_mod
from .manifest import list_addons

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
ODOO_CONF = "/etc/odoo/odoo.conf"


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
@click.option("--tail", "-t", default=100, show_default=True, type=int)
@click.option("--service", "-s", default=None, help="Service (default: web).")
def logs(project, follow, tail, service):
    """Show / stream logs."""
    _need_docker()
    slug, entry = _entry(project)
    svc = service or entry["services"]["web"]
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    args.append(svc)
    if follow:
        compose.live_output(entry["path"], *args)
    else:
        proc = compose.run(entry["path"], *args)
        click.echo(proc.stdout.decode(errors="replace"))


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


@main.command()
@click.argument("project")
@click.argument("module")
@click.option("--db", "-d", required=True, help="Database to upgrade the module in.")
@click.option("--keep-stopped", is_flag=True, help="Leave web stopped afterwards.")
def upgrade(project, module, db, keep_stopped):
    """Upgrade one addon (-u MODULE --stop-after-init), restarting web around it."""
    _need_docker()
    slug, entry = _entry(project)
    web = entry["services"]["web"]
    was_running = compose.web_running(entry["path"], entry)
    if was_running:
        click.echo(f"[{slug}] stopping web...")
        compose.run(entry["path"], "stop", web)
    cmd = ["odoo", "-c", ODOO_CONF, "-d", db, "-u", module, "--stop-after-init"]
    rc = compose.live_output(entry["path"], "run", "--rm", web, *cmd)
    if was_running and not keep_stopped:
        click.echo(f"[{slug}] starting web back...")
        compose.run(entry["path"], "start", web)
    if rc != 0:
        raise click.ClickException(f"[{slug}] upgrade failed (rc={rc}).")
    click.secho(f"[{slug}] upgraded {module} in {db}.", fg="green")


@main.command()
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to back up.")
def backup(project, db):
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
@click.argument("backup_path", type=click.Path(exists=True))
@click.option("--name", "-n", default=None, help="Restore under a different DB name.")
@click.option("--reset-admin/--no-reset-admin", "reset_admin_flag", default=True,
              help="Reset main user to admin/admin afterwards (default: yes).")
def restore(project, backup_path, name, reset_admin_flag):
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

    click.echo(f"[{slug}] restoring into '{target}'...")
    try:
        info = restore_mod.restore(entry["path"], entry, src, target)
    except (ValueError, compose.DockerError) as exc:
        raise click.ClickException(str(exc))
    if not info["filestore"]:
        click.secho("[!] No filestore found in backup - attachments will be missing.", fg="yellow")

    if reset_admin_flag:
        try:
            res = admin.reset_admin(entry["path"], entry, target)
            click.secho(
                f"[{slug}] admin reset: login 'admin' / 'admin' (user #{res['id']}, was '{res['old_login']}')",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    click.secho(f"[{slug}] restored into '{target}'. Restart web if it was running.", fg="green")


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
    testing.drop_db_if_exists(entry["path"], entry, db)
    compose.exec_service(
        entry["path"], "db", "createdb", "-U", entry.get("db_user", "odoo"), db,
    )
    click.secho(f"[{slug}] '{db}' recreated empty.", fg="green")


@main.command()
@click.argument("project")
@click.argument("module")
@click.option("--db", "-d", default=None, help="Test DB name (default: test_<module>).")
@click.option("--keep-db", is_flag=True, help="Keep the throwaway test database after the run.")
@click.option("--test-tags", "-t", default=None, help="Override --test-tags (default: /<module>).")
@click.option("--timeout", type=int, default=None, help="Seconds before giving up.")
def test(project, module, db, keep_db, test_tags, timeout):
    """Run an addon's tests in a disposable database."""
    _need_docker()
    slug, entry = _entry(project)
    addons_dir = entry.get("custom_addons")
    if not addons_dir or not (Path(addons_dir) / module / "__manifest__.py").exists():
        raise click.ClickException(f"Module '{module}' not found in {addons_dir}")

    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in module)[:40]
    dbname = db or f"test_{safe}"

    click.echo(f"[{slug}] running tests for {module} in throwaway DB '{dbname}'...")
    result = testing.run_tests(
        entry["path"], entry, module, dbname,
        test_tags=test_tags, keep_db=keep_db, timeout=timeout,
    )

    if result.ok:
        click.secho(f"[{slug}] PASS ({result.ran or '?'} tests)", fg="green")
    else:
        click.secho(f"[{slug}] FAIL", fg="red")
        for failure in result.failures[:20]:
            click.secho(f"   {failure}", fg="red")
        if result.raw_tail:
            click.echo("\n--- last log lines ---")
            click.echo(result.raw_tail)
    if result.log_path:
        click.echo(f"log: {result.log_path}")
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
