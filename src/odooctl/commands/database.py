import datetime
import json
import shutil
from dataclasses import replace
from pathlib import Path

import click

from .. import admin, compose, space, testing
from .. import icons as icons_mod
from .. import pull_workflow as pull_workflow_mod
from .. import restore as restore_mod
from .. import sanitize as sanitize_mod
from .common import entry, need_docker, pick_db
from .root import main


@main.command(section="Database")
@click.argument("project")
@click.option(
    "--from",
    "from_",
    required=False,
    default=None,
    help="SSH target, e.g. ssh://acme@acme.odoo.sh or user@server (key auth).",
)
@click.option(
    "--path", default=None, help="Remote backup path (default: newest in the standard backup folders)."
)
@click.option("--db", "-d", default=None, help="Database name for the restore.")
@click.option("--no-reset-admin", is_flag=True, help="Keep restored credentials as-is.")
@click.option("--keep-download", is_flag=True, help="Keep the downloaded bundle after restoring.")
@click.option(
    "--key",
    type=click.Path(exists=True),
    default=None,
    help="SSH private key file (e.g. ~/.ssh/id_ed25519_acme).",
)
@click.option(
    "--save",
    is_flag=True,
    help="Remember connection settings; afterwards use plain `odooctl pull PROJECT`.",
)
@click.option(
    "--with-filestore",
    is_flag=True,
    help="Also download attachments. The default downloads only the database dump.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip the overwrite confirmation.")
@click.option("--no-sanitize", is_flag=True, help="Skip database neutralization.")
@click.option(
    "--no-fix-icons",
    is_flag=True,
    help="Skip automatic menu icon repair when no filestore is restored.",
)
def pull(
    project,
    from_,
    path,
    db,
    no_reset_admin,
    keep_download,
    key,
    save,
    with_filestore,
    yes,
    no_sanitize,
    no_fix_icons,
):
    """Pull the latest backup over SSH and restore it."""
    need_docker()
    slug, project_entry = entry(project)
    try:
        options = pull_workflow_mod.PullOptions(
            ssh_target=from_,
            remote_path=path,
            database=db,
            ssh_key=str(Path(key).expanduser()) if key else None,
            with_filestore=with_filestore,
            reset_admin=not no_reset_admin,
            fix_icons=not no_fix_icons,
            sanitize=not no_sanitize,
            keep_download=keep_download,
            overwrite=yes,
            save_settings=save,
        )
        plan = pull_workflow_mod.plan_pull(slug, options)
        existing = pull_workflow_mod.check_database_ready(project_entry, plan.database)
        if plan.database in existing and not yes:
            click.confirm(f"Database '{plan.database}' already exists. DROP it and restore over it?", abort=True)
            options = replace(options, overwrite=True)
            plan = replace(plan, options=options)
        pull_workflow_mod.run_pull(slug, project_entry, options, plan=plan)
    except pull_workflow_mod.PullWorkflowError as exc:
        raise click.ClickException(str(exc)) from exc


@main.command(section="Database")
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to back up.")
@click.option(
    "--keep",
    "-k",
    type=int,
    default=None,
    help="Keep only the newest N snapshots of this database.",
)
def backup(project, db, keep):
    """Back up a database and its filestore."""
    need_docker()
    slug, project_entry = entry(project)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(project_entry["path"]) / "backups" / "odooctl" / f"{db}_{stamp}"
    destination.mkdir(parents=True, exist_ok=True)

    click.echo(f"[{slug}] dumping database '{db}'...")
    with open(destination / "db.dump", "wb") as output:
        compose.exec_service(
            project_entry["path"],
            "db",
            "pg_dump",
            "-U",
            project_entry.get("db_user", "odoo"),
            "-Fc",
            db,
            capture=False,
            stdout_file=output,
        )

    click.echo(f"[{slug}] archiving filestore...")
    with open(destination / "filestore.tar.gz", "wb") as output:
        compose.exec_service(
            project_entry["path"],
            "web",
            "tar",
            "czf",
            "-",
            "-C",
            "/var/lib/odoo/filestore",
            db,
            capture=False,
            stdout_file=output,
        )

    (destination / "meta.json").write_text(json.dumps({"database": db, "created": stamp}))
    size_mb = sum(item.stat().st_size for item in destination.iterdir()) / 1e6
    click.secho(f"[{slug}] backup saved to {destination} ({size_mb:.1f} MB)", fg="green")

    if keep is not None:
        old_snapshots = space.plan_backup_prunes(destination.parent, keep)
        for snapshot in old_snapshots:
            shutil.rmtree(snapshot, ignore_errors=True)
        if old_snapshots:
            click.echo(f"[{slug}] pruned {len(old_snapshots)} older snapshot(s) of '{db}' (kept {keep}).")
    click.echo(f"restore with: odooctl restore {slug} {destination}")


@main.command("reset-admin", section="Database")
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database name.")
@click.option("--login", "-l", default="admin", show_default=True)
@click.option("--password", "-p", default="admin", show_default=True)
@click.option("--user-id", "-u", type=int, default=None, help="Force a specific res_users id.")
def reset_admin(project, db, login, password, user_id):
    """Reset the main internal user's login and password."""
    need_docker()
    slug, project_entry = entry(project)
    try:
        info = admin.reset_admin(project_entry["path"], project_entry, db, login, password, user_id)
    except (compose.DockerError, RuntimeError) as exc:
        raise click.ClickException(str(exc))
    click.secho(
        f"[{slug}] user #{info['id']} ({info['name']}) was '{info['old_login']}' "
        f"-> now '{login}' / '{password}'",
        fg="green",
    )


@main.command("fix-icons", section="Database")
@click.argument("project")
@click.option("--db", "-d", default=None, help="Database to repair (default: picked automatically).")
def fix_icons(project, db):
    """Re-import missing menu icons from addon sources."""
    need_docker()
    slug, project_entry = entry(project)
    database = pick_db(project_entry, db)
    click.echo(f"[{slug}] repairing menu icons in '{database}'...")
    try:
        counts = icons_mod.fix_icons(project_entry["path"], project_entry, database)
    except (compose.DockerError, RuntimeError) as exc:
        raise click.ClickException(str(exc))
    click.secho(
        f"[{slug}] checked {counts.get('checked', 0)} menu icon(s), "
        f"re-imported {counts.get('fixed', 0)}, "
        f"{counts.get('unrepairable', 0)} without a source file.",
        fg="green",
    )
    if counts.get("fixed") or counts.get("unrepairable"):
        click.echo(
            f"[{slug}] restart web (`odooctl restart {slug}`) so it picks up the "
            "repaired icons (they are cached in the server and the browser)."
        )


@main.command(section="Database")
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to sanitize.")
@click.option("--names", is_flag=True, help="Also replace partner names with 'Partner #id'.")
@click.option("--keep-crons", is_flag=True, help="Leave scheduled actions enabled.")
@click.option("--keep-mail", is_flag=True, help="Skip mail queue purge and server disable.")
def sanitize(project, db, names, keep_crons, keep_mail):
    """Make a restored production database safe for local use."""
    need_docker()
    slug, project_entry = entry(project)
    click.echo(f"[{slug}] sanitizing '{db}'...")
    try:
        counts = sanitize_mod.sanitize(
            project_entry["path"],
            project_entry,
            db,
            with_names=names,
            keep_crons=keep_crons,
            keep_mail=keep_mail,
        )
    except (compose.DockerError, RuntimeError) as exc:
        raise click.ClickException(str(exc))
    for key, label in sanitize_mod.LABELS:
        if counts.get(key):
            click.secho(f"[{slug}] {counts[key]:>6}  {label}", fg="green")
    click.secho(f"[{slug}] '{db}' is now safe to work on.", fg="green")


@main.command(section="Database")
@click.argument("project")
@click.argument("backup_path", type=click.Path(exists=True))
@click.option("--name", "-n", default=None, help="Restore under a different DB name.")
@click.option(
    "--reset-admin/--no-reset-admin",
    "reset_admin_flag",
    default=True,
    help="Reset the main user to admin/admin afterwards.",
)
@click.option(
    "--sanitize/--no-sanitize",
    "sanitize_flag",
    default=False,
    help="Neutralize the restored database for local use.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip the overwrite confirmation.")
def restore(project, backup_path, name, reset_admin_flag, sanitize_flag, yes):
    """Restore an odooctl, pg_dump, or Odoo.sh backup."""
    need_docker()
    slug, project_entry = entry(project)
    source = Path(backup_path)
    target = restore_mod.target_name(source, restore_mod.detect_format(source), name)
    if not target:
        raise click.ClickException("Could not determine a database name. Hint: pass --name.")

    existing = compose.databases(project_entry["path"], project_entry.get("db_user", "odoo")) or []
    if target in existing and not yes:
        click.confirm(f"Database '{target}' already exists. DROP it and restore over it?", abort=True)

    was_running = compose.web_running(project_entry["path"], project_entry)
    if was_running:
        click.echo(f"[{slug}] stopping web for the restore...")
        compose.run(project_entry["path"], "stop", project_entry["services"]["web"])
    click.echo(f"[{slug}] restoring into '{target}'...")
    try:
        info = restore_mod.restore(project_entry["path"], project_entry, source, target)
    except (ValueError, compose.DockerError) as exc:
        if was_running:
            compose.run(project_entry["path"], "start", project_entry["services"]["web"])
        raise click.ClickException(str(exc))
    if not info["filestore"]:
        click.secho("[!] No filestore found in backup - attachments will be missing.", fg="yellow")
        try:
            counts = icons_mod.fix_icons(project_entry["path"], project_entry, target)
            click.secho(
                f"[{slug}] menu icons: checked {counts.get('checked', 0)}, "
                f"re-imported {counts.get('fixed', 0)}.",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] icon repair failed: {exc}", fg="yellow")
    for extension in info.get("skipped_extensions") or []:
        click.secho(f"[!] postgres extension '{extension}' not available locally - skipped", fg="yellow")

    if reset_admin_flag:
        try:
            result = admin.reset_admin(project_entry["path"], project_entry, target)
            click.secho(
                f"[{slug}] admin reset: login 'admin' / 'admin' "
                f"(user #{result['id']}, was '{result['old_login']}')",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    if was_running:
        click.echo(f"[{slug}] starting web back...")
        compose.run(project_entry["path"], "start", project_entry["services"]["web"])

    if sanitize_flag:
        try:
            counts = sanitize_mod.sanitize(project_entry["path"], project_entry, target)
            for key, label in sanitize_mod.LABELS:
                if counts.get(key):
                    click.secho(f"[{slug}] {counts[key]:>6}  {label}", fg="green")
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] sanitize failed: {exc}", fg="yellow")

    click.secho(f"[{slug}] restored into '{target}'.", fg="green")


@main.command(section="Database")
@click.argument("project")
@click.option("--db", "-d", required=True, help="Database to wipe.")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def reset(project, db, yes):
    """Drop a database and recreate it empty."""
    need_docker()
    slug, project_entry = entry(project)
    if not yes:
        click.confirm(f"Drop database '{db}' on {slug}? This cannot be undone.", abort=True)
    testing.cleanup_db_artifacts(project_entry["path"], project_entry, db)
    compose.exec_service(
        project_entry["path"],
        "db",
        "createdb",
        "-U",
        project_entry.get("db_user", "odoo"),
        db,
    )
    click.secho(f"[{slug}] '{db}' recreated empty (old filestore removed).", fg="green")
