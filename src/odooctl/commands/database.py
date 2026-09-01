import datetime
import json
import shutil
from pathlib import Path

import click

from .. import admin, compose, registry, space, testing
from .. import icons as icons_mod
from .. import pull as pull_mod
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
def pull(project, from_, path, db, no_reset_admin, keep_download, key, save, with_filestore, yes, no_sanitize):
    """Pull the latest backup over SSH and restore it."""
    need_docker()
    slug, project_entry = entry(project)
    saved = registry.load_pull_settings(slug)
    from_ = from_ or saved.get("from")
    if not from_:
        raise click.ClickException(
            "Pass --from ssh://user@host the first time.\nHint: add --save to remember it for future pulls."
        )
    path = path or saved.get("path")
    key = key or saved.get("key")
    db = db or saved.get("db")
    try:
        target, port = pull_mod.parse_target(from_)
    except pull_mod.PullError as exc:
        raise click.ClickException(str(exc))
    if save:
        registry.save_pull_settings(
            slug,
            {
                "from": from_,
                "path": path,
                "key": str(Path(key).expanduser()) if key else None,
                "db": db,
            },
        )
        click.echo(f"[{slug}] saved pull settings (next time: odooctl pull {slug}).")

    database = db or f"{slug}_pulled"
    if not db:
        click.secho(f"(no -d given; restoring as '{database}')", fg="yellow")

    state, _ = compose.service_state(project_entry["path"], project_entry["services"]["db"])
    if state != "running":
        raise click.ClickException(
            f"[{slug}] db container is not running.\nHint: start it with `odooctl up {slug}`."
        )
    existing = compose.databases(project_entry["path"], project_entry.get("db_user", "odoo")) or []
    if database in existing and not yes:
        click.confirm(f"Database '{database}' already exists. DROP it and restore over it?", abort=True)

    click.echo(f"[{slug}] looking for the latest backup on {target}...")
    try:
        remote = pull_mod.find_remote_backup(target, port=port, path=path, key=key)
        filestore_note = " (filestore available)" if remote.get("mirror") else " (dump only)"
        click.echo(f"[{slug}] found {remote['sql_gz']}{filestore_note}")
        if not with_filestore:
            click.echo(f"[{slug}] skipping filestore (--with-filestore to include attachments)")
        local = pull_mod.download(
            target,
            port,
            remote,
            Path(project_entry["path"]) / "backups" / "pulled",
            key=key,
            with_filestore=with_filestore,
        )
    except pull_mod.PullError as exc:
        raise click.ClickException(str(exc))
    size_mb = sum(item.stat().st_size for item in local.rglob("*") if item.is_file()) / 1e6
    click.echo(f"[{slug}] downloaded {local.name}/ ({size_mb:.1f} MB)")
    if local.is_dir():
        dumps = sorted(local.glob("*.sql.gz"))
        source_db = restore_mod._dump_create_target(dumps[0]) if dumps else None
        if source_db:
            click.echo(f"[{slug}] source database in dump: '{source_db}' -> will become '{database}'")

    was_running = compose.web_running(project_entry["path"], project_entry)
    if was_running:
        click.echo(f"[{slug}] stopping web for the restore...")
        compose.run(project_entry["path"], "stop", project_entry["services"]["web"])
    click.echo(f"[{slug}] restoring into '{database}'...")
    try:
        info = restore_mod.restore(project_entry["path"], project_entry, local, database)
    except (ValueError, compose.DockerError) as exc:
        if was_running:
            compose.run(project_entry["path"], "start", project_entry["services"]["web"])
        raise click.ClickException(f"restore failed: {exc}")
    if with_filestore and not info["filestore"]:
        click.secho("[!] no filestore found remotely - attachments missing", fg="yellow")
    for extension in info.get("skipped_extensions") or []:
        click.secho(
            f"[!] postgres extension '{extension}' not available locally - skipped "
            "(install it in your db image if you need it)",
            fg="yellow",
        )

    if not info["filestore"]:
        click.echo(f"[{slug}] no filestore - re-importing menu icons from addon sources...")
        try:
            counts = icons_mod.fix_icons(project_entry["path"], project_entry, database)
            click.secho(
                f"[{slug}] menu icons: checked {counts.get('checked', 0)}, "
                f"re-imported {counts.get('fixed', 0)}.",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] icon repair failed: {exc}", fg="yellow")

    if not no_reset_admin:
        try:
            result = admin.reset_admin(project_entry["path"], project_entry, database)
            click.secho(
                f"[{slug}] login ready: admin / admin  (user #{result['id']}, was '{result['old_login']}')",
                fg="green",
            )
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] reset-admin failed: {exc}", fg="yellow")

    if not no_sanitize:
        click.echo(f"[{slug}] sanitizing (neutralizing) '{database}'...")
        try:
            counts = sanitize_mod.sanitize(project_entry["path"], project_entry, database)
            for key, label in sanitize_mod.LABELS:
                if counts.get(key):
                    click.secho(f"[{slug}] {counts[key]:>6}  {label}", fg="green")
        except (compose.DockerError, RuntimeError) as exc:
            click.secho(f"[!] sanitize failed: {exc}", fg="yellow")

    if was_running:
        click.echo(f"[{slug}] starting web back...")
        compose.run(project_entry["path"], "start", project_entry["services"]["web"])

    if keep_download:
        click.echo(f"[{slug}] bundle kept at {local}")
    else:
        shutil.rmtree(local, ignore_errors=True)
        click.echo(f"[{slug}] cleaned up download.")

    http_port = project_entry.get("ports", {}).get("http")
    if http_port:
        click.secho(f"[{slug}] done -> http://localhost:{http_port}", fg="green")


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
