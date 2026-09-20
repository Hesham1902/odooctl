import shutil
import subprocess
from pathlib import Path

import click

from .. import admin, compose, onboarding, provision, pull_workflow, registry, space
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


def _default_parent_dir():
    existing_roots = [
        root for root in (registry.load_config().get("roots") or []) if Path(root).expanduser().is_dir()
    ]
    return existing_roots[0] if existing_roots else str(Path.cwd())


def _print_init_plan(plan, addon_specs, backup_path, backup_format, pull_options=None):
    click.secho(f"new project   : {plan['slug']}  ->  {plan['path']}", bold=True)
    click.echo(f"template      : {plan['template']} (Odoo {plan['version'] or '?'})")
    click.echo(f"containers    : {plan['container_names']['web']}, {plan['container_names']['db']}")
    click.echo(f"ports         : {plan['ports']}")
    click.echo(f"copying       : {', '.join(plan['copy'])}")
    if backup_path:
        click.echo(f"restore from  : {backup_path} ({backup_format})")
    if pull_options:
        remote = pull_options.ssh_target or "saved SSH target"
        remote_path = pull_options.remote_path or "newest standard backup"
        database = pull_options.database or f"{plan['slug']}_pulled"
        click.echo(f"remote pull   : {remote} ({remote_path}) -> {database}")
    if addon_specs:
        click.echo("addon repos   :")
        for spec in addon_specs:
            ref = f"#{spec.ref}" if spec.ref else ""
            target = "custom_addons/ (auto layout)" if len(addon_specs) == 1 else f"custom_addons/{spec.name}/"
            click.echo(f"  - {spec.url}{ref}  ->  {target}")


def _print_addon_results(slug, results):
    for r in results:
        if r.status in ("ok", "configured", "direct"):
            click.secho(f"[{slug}] {r.message}", fg="green")
        elif r.status in ("nested", "empty"):
            click.secho(f"[{slug}] warning: {r.message}", fg="yellow")
        elif r.status == "conflict":
            click.secho(f"[{slug}] skipped {r.spec.name}: {r.message}", fg="yellow")
        elif r.status == "error":
            click.secho(f"[{slug}] failed to clone {r.spec.url}: {r.message}", fg="red")
            click.echo(f"      recover: git clone {r.spec.url} custom_addons/{r.spec.name}   (run manually)")


def _run_init_wizard(
    *,
    version,
    template,
    from_,
    db,
    parent_dir,
    no_build,
    build,
    no_reset_admin,
    addon_repos,
    pull_from,
    pull_path,
    pull_key,
    pull_with_filestore,
    pull_no_reset_admin,
    pull_no_sanitize,
    pull_no_fix_icons,
    pull_keep_download,
    pull_yes,
    pull_save,
):
    """Prompt only for what wasn't already supplied via flags."""
    click.secho("odooctl init - guided setup (Ctrl+C to cancel)", bold=True)

    name = click.prompt("Project name").strip()
    while not provision.slugify(name):
        click.secho("Please enter a name with at least one letter or digit.", fg="yellow")
        name = click.prompt("Project name").strip()

    if not parent_dir:
        parent_dir = click.prompt("Parent directory", default=_default_parent_dir())

    if not from_ and not pull_from:
        answer = click.prompt(
            "Backup to restore (path, or leave blank to start empty)", default="", show_default=False
        ).strip()
        from_ = answer or None
        if not from_:
            if click.confirm("Pull the latest Odoo.sh backup over SSH instead?", default=False):
                pull_from = click.prompt("SSH target", default="ssh://1234567@your-project.odoo.com").strip()
                pull_path = click.prompt(
                    "Remote backup path (blank = newest standard backup)", default="", show_default=False
                ).strip() or None
                pull_key = click.prompt(
                    "Private key path (blank = SSH agent/default key)", default="", show_default=False
                ).strip() or None
                pull_with_filestore = click.confirm("Include filestore and attachments?", default=False)
                pull_no_reset_admin = not click.confirm("Reset admin/admin after restore?", default=True)
                pull_no_sanitize = not click.confirm("Sanitize the database for local development?", default=True)
                pull_no_fix_icons = not click.confirm("Repair missing menu icons?", default=True)
                pull_keep_download = click.confirm("Keep the downloaded backup bundle?", default=False)
                pull_yes = click.confirm("Replace an existing local database without asking?", default=False)
                pull_save = click.confirm("Remember these connection settings for future pulls?", default=True)

    if not version and not template:
        projects = registry.get_projects()
        versions = sorted({v for v in (registry.detect_version(e) for e in projects.values()) if v})
        inferred = None
        if from_:
            candidate = Path(from_).expanduser()
            if candidate.is_file():
                inferred = restore_mod.zip_server_version(candidate)
        if inferred:
            click.echo(f"Odoo version  : {inferred} (inferred from backup)")
            version = inferred
        elif len(versions) == 1:
            click.echo(f"Odoo version  : {versions[0]} (only one registered)")
            version = versions[0]
        elif versions:
            click.echo("Registered Odoo versions: " + ", ".join(versions))
            version = click.prompt("Odoo version", default=versions[-1])
        else:
            version = click.prompt("Odoo version (e.g. 18)")

    reset_admin = not no_reset_admin
    if from_:
        db = db or click.prompt("Database name for the restore", default=f"{provision.slugify(name)}_db")
        reset_admin = not no_reset_admin and click.confirm("Reset admin/admin after restore?", default=True)
    elif pull_from:
        db = db or click.prompt("Database name for the pull", default=f"{provision.slugify(name)}_pulled")

    addon_repos = list(addon_repos or ())
    if click.confirm("Add an addon repository to clone into custom_addons/?", default=False):
        while True:
            url = click.prompt("  Repo URL (git clone target)").strip()
            ref = click.prompt("  Branch/tag (blank = default branch)", default="", show_default=False).strip()
            addon_repos.append(f"{url}#{ref}" if ref else url)
            if not click.confirm("Add another addon repository?", default=False):
                break

    return {
        "name": name,
        "version": version,
        "template": template,
        "from_": from_,
        "db": db,
        "parent_dir": parent_dir,
        "no_build": no_build,
        "build": build,
        "no_reset_admin": not reset_admin,
        "addon_repos": tuple(addon_repos),
        "pull_from": pull_from,
        "pull_path": pull_path,
        "pull_key": pull_key,
        "pull_with_filestore": pull_with_filestore,
        "pull_no_reset_admin": pull_no_reset_admin,
        "pull_no_sanitize": pull_no_sanitize,
        "pull_no_fix_icons": pull_no_fix_icons,
        "pull_keep_download": pull_keep_download,
        "pull_yes": pull_yes,
        "pull_save": pull_save,
    }


@main.command(section="Project management")
@click.argument("name", required=False)
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
@click.option(
    "--pull-from",
    default=None,
    help="Pull the latest Odoo.sh backup over SSH after the new project starts.",
)
@click.option("--pull-path", default=None, help="Remote backup path for --pull-from (default: newest standard backup).")
@click.option(
    "--pull-key",
    type=click.Path(exists=True),
    default=None,
    help="SSH private key file for --pull-from.",
)
@click.option("--pull-with-filestore", is_flag=True, help="Include attachments when using --pull-from.")
@click.option("--pull-no-reset-admin", is_flag=True, help="Keep remote credentials when using --pull-from.")
@click.option("--pull-no-sanitize", is_flag=True, help="Skip local sanitization when using --pull-from.")
@click.option("--pull-no-fix-icons", is_flag=True, help="Skip menu icon repair when using --pull-from.")
@click.option("--pull-keep-download", is_flag=True, help="Keep the downloaded bundle when using --pull-from.")
@click.option("--pull-yes", is_flag=True, help="Replace an existing local database without asking when pulling.")
@click.option("--pull-save", is_flag=True, help="Remember SSH settings for future `odooctl pull PROJECT` runs.")
@click.option(
    "--addon-repo",
    "addon_repos",
    multiple=True,
    metavar="URL[#REF]",
    help="Clone a git addon repo into custom_addons/ before starting (repeatable). "
    "REF is a branch/tag, e.g. https://github.com/OCA/queue.git#18.0.",
)
@click.option("--dry-run", is_flag=True, help="Show the plan without creating anything.")
def init(
    name,
    version,
    template,
    from_,
    db,
    parent_dir,
    no_build,
    build,
    no_reset_admin,
    pull_from,
    pull_path,
    pull_key,
    pull_with_filestore,
    pull_no_reset_admin,
    pull_no_sanitize,
    pull_no_fix_icons,
    pull_keep_download,
    pull_yes,
    pull_save,
    addon_repos,
    dry_run,
):
    """Bootstrap a new local Odoo project from an existing one.

    Run with no NAME in an interactive terminal for a guided setup:

        odooctl init

    Or pass NAME plus flags for scripts/automation:

        odooctl init acme --version 18 --parent-dir ~/work

        odooctl init acme --from ~/Downloads/acme.zip --addon-repo https://github.com/OCA/queue.git#18.0

        odooctl init acme --version 18 --pull-from ssh://1234567@acme.odoo.com
    """
    interactive = name is None
    if interactive:
        if not onboarding.stdin_is_interactive():
            raise click.ClickException(
                "odooctl init needs a NAME (plus --version or --template) when stdin isn't a terminal.\n"
                "Examples:\n"
                "  odooctl init acme --version 18 --parent-dir ~/work\n"
                "  odooctl init acme --from ~/Downloads/acme.zip\n"
                "Hint: run `odooctl init` with no NAME in a real terminal for the guided setup."
            )
        answers = _run_init_wizard(
            version=version,
            template=template,
            from_=from_,
            db=db,
            parent_dir=parent_dir,
            no_build=no_build,
            build=build,
            no_reset_admin=no_reset_admin,
            addon_repos=addon_repos,
            pull_from=pull_from,
            pull_path=pull_path,
            pull_key=pull_key,
            pull_with_filestore=pull_with_filestore,
            pull_no_reset_admin=pull_no_reset_admin,
            pull_no_sanitize=pull_no_sanitize,
            pull_no_fix_icons=pull_no_fix_icons,
            pull_keep_download=pull_keep_download,
            pull_yes=pull_yes,
            pull_save=pull_save,
        )
        name = answers["name"]
        version = answers["version"]
        template = answers["template"]
        from_ = answers["from_"]
        db = answers["db"]
        parent_dir = answers["parent_dir"]
        no_build = answers["no_build"]
        build = answers["build"]
        no_reset_admin = answers["no_reset_admin"]
        addon_repos = answers["addon_repos"]
        pull_from = answers["pull_from"]
        pull_path = answers["pull_path"]
        pull_key = answers["pull_key"]
        pull_with_filestore = answers["pull_with_filestore"]
        pull_no_reset_admin = answers["pull_no_reset_admin"]
        pull_no_sanitize = answers["pull_no_sanitize"]
        pull_no_fix_icons = answers["pull_no_fix_icons"]
        pull_keep_download = answers["pull_keep_download"]
        pull_yes = answers["pull_yes"]
        pull_save = answers["pull_save"]

    if from_ and pull_from:
        raise click.ClickException("Choose either a local --from backup or --pull-from SSH target, not both.")
    if any((pull_path, pull_key, pull_with_filestore, pull_no_reset_admin, pull_no_sanitize, pull_no_fix_icons,
            pull_keep_download, pull_yes)) and not pull_from:
        raise click.ClickException("--pull-* options require --pull-from SSH_TARGET.")

    need_docker()

    try:
        addon_specs = onboarding.parse_addon_repo_specs(addon_repos)
    except onboarding.InitError as exc:
        raise click.ClickException(str(exc))

    if addon_specs and shutil.which("git") is None:
        raise click.ClickException(
            str(
                onboarding.InitError(
                    "git is required to clone --addon-repo repositories but wasn't found on PATH.",
                    hint="Install git, or drop --addon-repo and add the addon folder manually.",
                )
            )
        )

    normalized_version = registry.normalize_version(version) if version else None
    backup_path = Path(from_) if from_ else None
    backup_format = None
    if backup_path:
        try:
            backup_format = onboarding.validate_backup(backup_path)
        except onboarding.InitError as exc:
            raise click.ClickException(str(exc))
        if backup_path.is_file() and not normalized_version and not template:
            inferred = restore_mod.zip_server_version(backup_path)
            if inferred:
                click.echo(f"inferred Odoo {inferred} from backup manifest")
                normalized_version = inferred

    pull_options = None
    if pull_from:
        pull_options = pull_workflow.PullOptions(
            ssh_target=pull_from,
            remote_path=pull_path,
            database=db,
            ssh_key=str(Path(pull_key).expanduser()) if pull_key else None,
            with_filestore=pull_with_filestore,
            reset_admin=not pull_no_reset_admin,
            fix_icons=not pull_no_fix_icons,
            sanitize=not pull_no_sanitize,
            keep_download=pull_keep_download,
            overwrite=pull_yes,
            save_settings=pull_save,
        )
        try:
            pull_workflow.plan_pull(provision.slugify(name), pull_options)
        except pull_workflow.PullWorkflowError as exc:
            raise click.ClickException(f"Cannot plan remote pull: {exc}") from exc

    if not parent_dir:
        parent_dir = _default_parent_dir()

    def _plan(dry):
        try:
            return provision.init_project(
                name, parent_dir, version=normalized_version, template_slug=template, dry_run=dry
            )
        except RuntimeError as exc:
            raise click.ClickException(str(onboarding.wrap_provision_error(exc)))

    if interactive and not dry_run:
        preview_plan, _ = _plan(True)
        _print_init_plan(preview_plan, addon_specs, backup_path, backup_format, pull_options)
        click.confirm("\nCreate this project?", abort=True)
        plan, project_entry = _plan(False)
    else:
        plan, project_entry = _plan(dry_run)
        _print_init_plan(plan, addon_specs, backup_path, backup_format, pull_options)
        if dry_run:
            click.echo("(dry run - nothing created)")
            return

    slug = plan["slug"]

    if addon_specs:
        click.echo(f"\n[{slug}] cloning addon repositories...")
        addon_results = onboarding.clone_addon_repos(project_entry["custom_addons"], addon_specs)
        onboarding.configure_nested_addon_paths(project_entry, addon_results)
        _print_addon_results(slug, addon_results)

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
    elif pull_options:
        click.echo(f"\n[{slug}] pulling the latest remote backup...")
        try:
            pull_workflow.run_pull(slug, project_entry, pull_options)
        except pull_workflow.PullWorkflowError as exc:
            raise click.ClickException(
                f"remote pull failed: {exc}\n"
                f"The project was created and is still available at {project_entry['path']}."
            ) from exc

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
