from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click

from .. import compose, registry, space
from .common import entry, measure, need_docker
from .root import main

GC_KEEP_BACKUPS = 3
GC_KEEP_LOGS = 20


def _count(value):
    return value if value is not None else "?"


def _image_usage(task):
    slug, project_entry, role = task
    try:
        image_ref = compose.find_built_image(project_entry, role)
        if not image_ref:
            return None
        image_id, size = space.image_identity(image_ref)
        return slug, role, image_ref, image_id, size
    except (compose.DockerError, space.SpaceError):
        return None


@main.command("space", aliases=("df",), section="Storage")
@click.argument("project", required=False)
def space_cmd(project):
    """Show Docker disk usage per project and globally."""
    need_docker()
    if project:
        slug, project_entry = entry(project)
        items = {slug: project_entry}
    else:
        items = registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet.\nHint: run `odooctl discover` to find projects.")

    try:
        with measure("docker disk summary"):
            totals = space.system_df()
            dangling = space.dangling_images()
    except space.SpaceError as exc:
        raise click.ClickException(str(exc))
    image_totals = totals.get("images", {})
    volume_totals = totals.get("local volumes", {})
    build_totals = totals.get("build cache", {})
    quick_win = (build_totals.get("reclaim_bytes") or 0) + sum(image["size_bytes"] or 0 for image in dangling)

    tasks = [
        (slug, project_entry, role) for slug, project_entry in sorted(items.items()) for role in ("web", "db")
    ]
    with measure("project image inspection"):
        if len(tasks) == 1:
            resolved = [_image_usage(tasks[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as executor:
                resolved = list(executor.map(_image_usage, tasks))
    usages = sorted((usage for usage in resolved if usage), key=lambda usage: usage[:2])
    shared_groups = space.group_shared_image_usage(usages)
    shared_savings = sum(
        group["size"] * (len(group["users"]) - 1)
        for group in shared_groups.values()
        if group["size"] and len(group["users"]) > 1
    )
    referenced_ids = {usage[3] for usage in usages if usage[3]}

    try:
        all_images = space.list_images()
    except space.SpaceError:
        all_images = []
    untracked = space.filter_untracked(all_images, referenced_ids) if all_images else []
    untracked_bytes = sum(image["size_bytes"] or 0 for image in untracked)

    click.secho("GLOBAL DOCKER", bold=True)
    click.echo(
        f"  images      {space.fmt_bytes(image_totals.get('size_bytes'))}"
        f"  ({_count(image_totals.get('total'))} total / "
        f"{_count(image_totals.get('active'))} in use)"
    )
    if shared_savings:
        click.echo(f"              ({space.fmt_bytes(shared_savings)} already saved via layer sharing)")
    click.echo(
        f"  volumes     {space.fmt_bytes(volume_totals.get('size_bytes'))}  "
        f"({_count(volume_totals.get('total'))})"
    )
    click.echo(
        f"  build cache {space.fmt_bytes(build_totals.get('size_bytes'))}"
        f"  (cleanable {space.fmt_bytes(build_totals.get('reclaim_bytes'))})"
    )
    dangling_bytes = sum(image["size_bytes"] or 0 for image in dangling)
    click.echo(f"  dangling    {len(dangling)} image(s), {space.fmt_bytes(dangling_bytes)}")
    if untracked:
        shown = sorted(untracked, key=lambda image: image["size_bytes"] or 0, reverse=True)[:4]
        extra = len(untracked) - len(shown)
        listing = "; ".join(f"{image['tag']} {space.fmt_bytes(image['size_bytes'])}" for image in shown)
        if extra > 0:
            listing += f"; +{extra} more"
        click.echo(f"  untracked   {len(untracked)} tagged image(s), {space.fmt_bytes(untracked_bytes)}")
        click.echo(f"              {listing}")
        click.secho(
            "              (gc --stale-images removes these; bases may re-download on next build)",
            fg="bright_black",
        )
    click.secho(f"  quick win   ~{space.fmt_bytes(quick_win)} -> odooctl gc --apply\n", fg="cyan")

    grand_total = 0
    with measure("project storage scan"):
        for slug, project_entry in sorted(items.items()):
            path = Path(project_entry["path"])
            version = registry.detect_version(project_entry) or "?"
            if not path.is_dir():
                click.secho(f"{slug}  ({path})", bold=True)
                click.echo("  folder missing - run `odooctl discover` to refresh the registry\n")
                continue
            click.secho(f"{slug}  ({path})  odoo {version}", bold=True)
            subtotal = 0

            for role in ("web", "db"):
                usage = next((value for value in usages if value[0] == slug and value[1] == role), None)
                label = f"{role} image"
                if usage is None:
                    click.echo(f"  {label:<11} not built")
                    continue
                _, _, image_ref, image_id, size = usage
                users = len(shared_groups[image_id]["users"]) if image_id in shared_groups else 1
                marker = f"  (shared ×{users})" if users > 1 else ""
                click.echo(f"  {label:<11} {image_ref}  {space.fmt_bytes(size)}{marker}")
                subtotal += size or 0

            try:
                volumes = space.project_volume_sizes(slug)
            except space.SpaceError:
                volumes = {}
            if volumes:
                joined = " | ".join(
                    f"{name} {space.fmt_bytes(size)}" for name, size in sorted(volumes.items())
                )
                click.echo(f"  volumes     {joined}")
                subtotal += sum(volumes.values())

            binds = []
            try:
                for host_path, label in space.bind_mounts(project_entry):
                    binds.append((host_path, label, space.du_bytes(host_path)))
            except Exception:
                binds = []
            if binds:

                def display(bind_path):
                    try:
                        return str(bind_path.relative_to(path))
                    except ValueError:
                        return bind_path.name

                joined = " | ".join(
                    f"{label} {display(bind_path)} {space.fmt_bytes(size)}"
                    for bind_path, label, size in sorted(binds, key=lambda bind: bind[2], reverse=True)
                )
                click.echo(f"  bind mounts {joined}")
                subtotal += sum(bind[2] for bind in binds)

            backups_dir = path / "backups" / "odooctl"
            backup_groups = space.backup_groups(backups_dir)
            snapshot_count = sum(len(snapshots) for snapshots in backup_groups.values())
            backups_size = space.du_bytes(backups_dir)
            click.echo(
                f"  backups     {space.fmt_bytes(backups_size)}  ({snapshot_count} snapshot(s))"
                f"   [gc keeps newest {GC_KEEP_BACKUPS}/db]"
            )
            logs_dir = path / "backups" / "test_logs"
            log_count = len([log for log in logs_dir.glob("*") if log.is_file()]) if logs_dir.is_dir() else 0
            logs_size = space.du_bytes(logs_dir)
            click.echo(
                f"  test logs   {space.fmt_bytes(logs_size)}  ({log_count} file(s))"
                f"   [gc keeps newest {GC_KEEP_LOGS}]"
            )
            subtotal += backups_size + logs_size
            grand_total += subtotal
            click.secho(f"  total       ~{space.fmt_bytes(subtotal)}", fg="bright_black")
            click.echo()

    if len(items) > 1:
        note = " (images counted per project; shared layers exist)" if shared_savings else ""
        click.secho(f"projects total ~{space.fmt_bytes(grand_total)}{note}", bold=True)


def _deep_cleanup(project, yes):
    slug, project_entry = entry(project)
    try:
        volumes = space.project_volume_sizes(slug)
    except space.SpaceError as exc:
        raise click.ClickException(str(exc))
    total = sum(volumes.values())
    names = ", ".join(sorted(volumes)) or "(no named volumes)"
    click.secho(
        f"This deletes every database and filestore of '{slug}' ({names}, {space.fmt_bytes(total)}).",
        fg="red",
        bold=True,
    )
    click.echo("Bind-mounted folders (addons, config, backups) are NOT touched.")
    if not yes:
        click.confirm("Wipe and recreate?", abort=True)
    item = space.GCItem(
        kind="deep-volumes",
        description="wipe project volumes",
        target=names,
        entry=project_entry,
    )
    space.execute([item])
    click.secho(f"[{slug}] volumes wiped. Run `odooctl up {slug}` for a fresh environment.", fg="green")


@main.command(section="Storage")
@click.argument("project", required=False)
@click.option("--apply", is_flag=True, help="Execute cleanup (default: show a dry-run plan).")
@click.option(
    "--keep-backups",
    type=int,
    default=3,
    show_default=True,
    help="Backup snapshots to keep per database.",
)
@click.option(
    "--keep-logs",
    type=int,
    default=20,
    show_default=True,
    help="Test log files to keep per project.",
)
@click.option(
    "--stale-images",
    is_flag=True,
    help="Also remove tagged images no registered project uses.",
)
@click.option(
    "--deep",
    is_flag=True,
    help="Wipe every named volume for PROJECT instead of normal cleanup.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation with --deep.")
def gc(project, apply, keep_backups, keep_logs, stale_images, deep, yes):
    """Plan or remove reclaimable project and Docker data."""
    need_docker()
    if deep:
        if not project:
            raise click.ClickException("--deep requires PROJECT.")
        if apply or stale_images:
            raise click.ClickException("--deep cannot be combined with --apply or --stale-images.")
        _deep_cleanup(project, yes)
        return

    if yes:
        raise click.ClickException("--yes is only valid with --deep.")
    if project:
        slug, project_entry = entry(project)
        items = {slug: project_entry}
    else:
        items = registry.get_projects()
    if not items:
        raise click.ClickException("Nothing registered yet.\nHint: run `odooctl discover` to find projects.")

    try:
        plan = space.build_gc_plan(projects=items, keep_backups=keep_backups, keep_logs=keep_logs)
        if stale_images:
            referenced = set()
            for project_entry in items.values():
                for role in ("web", "db"):
                    try:
                        image_ref = compose.find_built_image(project_entry, role)
                        if image_ref:
                            image_id, _ = space.image_identity(image_ref)
                            if image_id:
                                referenced.add(image_id)
                    except (compose.DockerError, space.SpaceError):
                        continue
            for image in space.filter_untracked(space.list_images(), referenced):
                plan.append(
                    space.GCItem(
                        kind="rmi",
                        description=f"remove unused image {image['tag']}",
                        target=image["id"],
                        bytes_free=image["size_bytes"],
                    )
                )
    except (space.SpaceError, compose.DockerError) as exc:
        raise click.ClickException(str(exc))

    if not plan:
        click.secho("Nothing to clean - your environments are already tidy.", fg="green")
        return

    total = sum(item.bytes_free or 0 for item in plan)
    click.secho(f"Cleanup plan ({len(plan)} item(s), ~{space.fmt_bytes(total)} reclaimable):", bold=True)
    current_project = None
    for item in plan:
        if item.project != current_project:
            current_project = item.project
            click.secho(f"[{current_project or 'global'}]", fg="cyan")
        click.echo(f"  - {item.description:<55} {item.size_label:>10}  {item.target}")
    click.echo()

    if not apply:
        click.echo("Dry run only. Re-run with --apply to execute.")
        return

    freed = space.execute(plan, echo=lambda message: click.echo(message))
    click.secho(
        f"Reclaimed at least ~{space.fmt_bytes(freed)}. "
        "On macOS the Docker Desktop disk shrinks lazily; restart Docker Desktop if needed.",
        fg="green",
    )


@main.command("gc-deep", hidden=True, section="Storage")
@click.argument("project")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def gc_deep_legacy(project, yes):
    """Compatibility alias for ``gc PROJECT --deep``."""
    need_docker()
    _deep_cleanup(project, yes)
