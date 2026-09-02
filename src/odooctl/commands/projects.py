import shutil
import subprocess
from pathlib import Path

import click

from .. import admin, compose, provision, registry, space
from .. import restore as restore_mod
from .common import entry, need_docker, print_project_line, wait_http
from .root import main


def _format_report(report):
    lines = ["Scan roots:"]
    labels = {}
    for root in report.roots:
        labels[root] = f"{root} (cwd, not saved)" if root in report.ephemeral else root
    width = max((len(label) for label in labels.values()), default=0)
    for root, count in report.roots.items():
        if count is None:
            status = "missing"
        else:
            status = f"scanned, {count} compose file{'s' if count != 1 else ''}"
        lines.append(f"  {labels[root]:<{width}}  {status}")
    if report.rejected:
        lines.append("Compose files seen but rejected:")
        for path, reason in report.rejected:
            lines.append(f"  {path}")
            lines.append(f"      {reason}")
    return "\n".join(lines)


def _empty_hint(report):
    depth = registry.MAX_PROJECT_DEPTH
    names = " / ".join(compose.COMPOSE_NAMES)
    if report.roots and len(report.missing_roots) == len(report.roots):
        return (
            "Hint: none of the scan roots exist. Add the folder that holds your projects: "
            "`odooctl discover --root /path/to/projects`."
        )
    seen = report.compose_files_seen
    if seen == 0:
        return (
            f"Hint: no compose files ({names}) under any root, up to {depth} folders deep. "
            "Add the folder directly above your projects with `odooctl discover --root /path`."
        )
    return (
        f"Hint: {seen} compose file{'s were' if seen != 1 else ' was'} found but none looked like an "
        "Odoo project (need one service mentioning 'odoo' and one Postgres service) - see reasons "
        f"above. Projects are matched up to {depth} folders below a root. "
        "Drop stale roots with `odooctl discover --forget-root PATH`."
    )


@main.command(section="Project management")
@click.option("--root", multiple=True, help="Extra directory to scan for projects (saved).")
@click.option("--forget-root", multiple=True, help="Stop scanning this saved root.")
@click.option("--verbose", "-v", is_flag=True, help="Show every root scanned and every compose file skipped.")
def discover(root, forget_root, verbose):
    """(Re)scan your work folders for Odoo Docker projects.

    The current directory is always scanned too, but only saved roots are remembered.
    """
    config, report = registry.refresh_registry(roots=root or None, forget=forget_root)
    projects = config["projects"]
    if not projects:
        raise click.ClickException(
            "No Odoo projects found.\n" + _format_report(report) + "\n" + _empty_hint(report)
        )
    if verbose:
        click.echo(_format_report(report))
        click.echo()
    click.echo(f"Found {len(projects)} project(s):")
    for slug, project_entry in sorted(projects.items()):
        print_project_line(slug, project_entry)
    if report.rejected and not verbose:
        count = len(report.rejected)
        click.echo(
            f"({count} compose file{'s' if count != 1 else ''} skipped - run `odooctl discover -v` to see why)"
        )
    for ephemeral in sorted(report.ephemeral):
        if any(Path(p["path"]).is_relative_to(ephemeral) for p in projects.values()):
            click.echo(f"Found under current directory; keep it with: odooctl discover --root {ephemeral}")


@main.command("projects", section="Project management")
def projects_cmd():
    """List registered projects."""
    projects = registry.get_projects()
    if not projects:
        raise click.ClickException("Nothing registered yet.\nHint: run `odooctl discover` to find projects.")
    for slug, project_entry in sorted(projects.items()):
        print_project_line(slug, project_entry)


@main.command(section="Project management")
@click.argument("name")
@click.option(
    "--version", "-v", default=None, help="Odoo version, e.g. 18 or 16.0 (or inferred from backup zip)."
)
@click.option("--template", "-t", default=None, help="Copy Docker setup from this registered project.")
@click.option(
    "--from",
    "from_",
    type=click.Path(exists=True),
    default=None,
    help="Backup to restore (.zip from Odoo.sh, .dump, or odooctl backup dir).",
)
@click.option("--db", "-d", default=None, help="Database name for the restore.")
@click.option(
    "--parent-dir",
    "-p",
    type=click.Path(file_okay=False),
    default=None,
    help="Folder to create the project in (default: first scan root).",
)
@click.option("--no-build", is_flag=True, help="Start without building anything.")
@click.option(
    "--build",
    is_flag=True,
    help="Force a fresh image build (default: reuse the template's image).",
)
@click.option("--no-reset-admin", is_flag=True, help="Skip admin/admin reset after restore.")
@click.option("--dry-run", is_flag=True, help="Show the plan without creating anything.")
def init(name, version, template, from_, db, parent_dir, no_build, build, no_reset_admin, dry_run):
    """Bootstrap a new local Odoo project from an existing one."""
    need_docker()
    normalized_version = registry.normalize_version(version) if version else None
    backup_path = Path(from_) if from_ else None
    if backup_path and backup_path.is_file() and not normalized_version and not template:
        inferred = restore_mod.zip_server_version(backup_path)
        if inferred:
            click.echo(f"inferred Odoo {inferred} from backup manifest")
            normalized_version = inferred

    if not parent_dir:
        existing_roots = [
            root for root in (registry.load_config().get("roots") or []) if Path(root).expanduser().is_dir()
        ]
        parent_dir = existing_roots[0] if existing_roots else str(Path.cwd())

    try:
        plan, project_entry = provision.init_project(
            name,
            parent_dir,
            version=normalized_version,
            template_slug=template,
            dry_run=dry_run,
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
    template_slug, _ = registry.resolve(plan["template"])
    template_entry = registry.get_projects()[template_slug]
    reused_image = None
    if not build and not no_build:
        web_image = compose.find_built_image(template_entry, "web")
        db_image = compose.find_built_image(template_entry, "db")
        if web_image and db_image:
            for source, destination in ((web_image, f"{slug}-web"), (db_image, f"{slug}-db")):
                subprocess.run(["docker", "tag", source, destination], check=True)
            reused_image = f"{web_image} -> {slug}-web"

    if no_build:
        compose.run(project_entry["path"], "up", "-d", "--no-build")
    elif reused_image:
        click.echo(f"[{slug}] reusing template image ({reused_image}) - skipping build")
        compose.run(project_entry["path"], "up", "-d", "--no-build")
    else:
        click.echo(f"\n[{slug}] building images (first time only, ~5-10 min)...")
        compose.run(project_entry["path"], "up", "-d", "--build")

    if backup_path:
        database = restore_mod.target_name(backup_path, restore_mod.detect_format(backup_path), db)
        if not database:
            database = f"{slug}_db"
            click.secho(f"(no name in backup; using '{database}')", fg="yellow")
        click.echo(f"[{slug}] restoring {backup_path.name} into '{database}'...")
        try:
            info = restore_mod.restore(project_entry["path"], project_entry, backup_path, database)
        except (ValueError, compose.DockerError) as exc:
            raise click.ClickException(f"restore failed: {exc}")
        if not info["filestore"]:
            click.secho("[!] no filestore in backup - attachments missing", fg="yellow")
        if not no_reset_admin:
            click.echo(f"[{slug}] resetting admin credentials...")
            try:
                result = admin.reset_admin(project_entry["path"], project_entry, database)
                click.secho(
                    f"[{slug}] login ready: admin / admin  "
                    f"(user #{result['id']}, was '{result['old_login']}')",
                    fg="green",
                )
            except (compose.DockerError, RuntimeError) as exc:
                click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    port = project_entry.get("ports", {}).get("http")
    click.echo(f"\n[{slug}] waiting for Odoo to boot (first boot can take a minute)...")
    if port and wait_http(port, timeout=300):
        click.secho(f"[{slug}] ready -> http://localhost:{port}", fg="green", bold=True)
    else:
        click.secho(f"[{slug}] still booting; check `odooctl logs {slug} -f`.", fg="yellow")
    click.echo(f"next: odooctl logs {slug} -f   |   odooctl open {slug}")


@main.command(section="Project management")
@click.argument("project")
@click.option(
    "--images",
    is_flag=True,
    help="Also remove project images (kept when another project shares them).",
)
@click.option(
    "--purge-folder",
    is_flag=True,
    help="Also DELETE the project folder from disk (source code, backups, data).",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts.")
def remove(project, images, purge_folder, yes):
    """Stop, unregister, and optionally delete a project."""
    slug, project_entry = entry(project)
    path = Path(project_entry["path"])
    if not yes:
        click.secho(f"Removing '{slug}' ({path}) from odooctl.", bold=True)

    if path.is_dir():
        need_docker()
        try:
            web_state, _ = compose.service_state(path, project_entry["services"]["web"])
            db_state, _ = compose.service_state(path, project_entry["services"]["db"])
            containers_exist = bool(web_state or db_state)
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
                image_ref = compose.find_built_image(project_entry, role)
                if not image_ref:
                    continue
                image_id, size = space.image_identity(image_ref)
            except (compose.DockerError, space.SpaceError):
                continue
            if not image_id:
                continue
            short_id = space.short_id(image_id)
            sharers = []
            for other_slug, other_entry in registry.get_projects().items():
                if other_slug == slug:
                    continue
                try:
                    other_ref = compose.find_built_image(other_entry, role)
                    other_id, _ = space.image_identity(other_ref) if other_ref else (None, None)
                except (compose.DockerError, space.SpaceError):
                    continue
                if other_id and space.short_id(other_id) == short_id:
                    sharers.append(other_slug)
            if sharers:
                click.echo(
                    f"[{slug}] keeping {image_ref} ({space.fmt_bytes(size)}) - "
                    f"shared with {', '.join(sharers)}"
                )
                continue
            subprocess.run(["docker", "rmi", image_ref], capture_output=True)
            click.echo(f"[{slug}] removed image {image_ref} ({space.fmt_bytes(size)})")

    if purge_folder and path.is_dir():
        if not yes:
            click.secho(f"About to DELETE {path} permanently (code, data/, backups).", fg="red", bold=True)
            click.confirm("This cannot be undone. Delete the folder?", abort=True)
        shutil.rmtree(path, ignore_errors=True)
        click.echo(f"[{slug}] folder deleted.")

    registry.unregister(slug)
    click.secho(f"[{slug}] removed from odooctl.", fg="green")
