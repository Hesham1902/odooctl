import json
import sys
from pathlib import Path

import click

from .. import compose, dbdiff, testing, vcs
from .. import deps as deps_mod
from ..manifest import list_addons
from .common import ODOO_CONF, entry, need_docker, pick_db
from .root import main


@main.command(section="Development")
@click.argument("project")
@click.option("--db", "-d", default=None, help="Database (default: postgres maintenance db).")
def psql(project, db):
    """Open an interactive PostgreSQL session."""
    need_docker()
    _, project_entry = entry(project)
    compose.run(
        project_entry["path"],
        "exec",
        project_entry["services"]["db"],
        "psql",
        "-U",
        project_entry.get("db_user", "odoo"),
        "-d",
        db or "postgres",
        capture=False,
        check=False,
    )


@main.command(section="Development")
@click.argument("project")
@click.option("--db", "-d", default=None, help="Database to open the shell on.")
def shell(project, db):
    """Open an interactive Odoo ORM shell."""
    need_docker()
    slug, project_entry = entry(project)
    if not compose.web_running(project_entry["path"], project_entry):
        raise click.ClickException(
            f"[{slug}] web container is not running.\nHint: start it with `odooctl up {slug}`."
        )
    database = pick_db(project_entry, db)
    click.echo(f"[{slug}] odoo shell on '{database}' (exit with exit() or Ctrl-D)")
    compose.run(
        project_entry["path"],
        "exec",
        project_entry["services"]["web"],
        "odoo",
        "shell",
        "-c",
        ODOO_CONF,
        "-d",
        database,
        "--no-http",
        capture=False,
        check=False,
    )


@main.command(section="Development")
@click.argument("project")
@click.argument("module")
def deps(project, module):
    """Show an addon's dependency tree, reverse dependencies, and cycles."""
    slug, project_entry = entry(project)
    addons_dir = project_entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException(f"{slug} has no custom_addons folder registered.")
    addons = list_addons(addons_dir)
    if module not in addons:
        raise click.ClickException(
            f"Module '{module}' not found in {addons_dir}.\nOn disk: {', '.join(sorted(addons)) or '(none)'}"
        )
    graph = deps_mod.build_graph(addons)

    def render(current_module, prefix="", seen=None):
        seen = seen if seen is not None else {module}
        children = [
            dependency for dependency in graph.get(current_module, []) if dependency != current_module
        ]
        for index, dependency in enumerate(children):
            last = index == len(children) - 1
            branch = "`-- " if last else "|-- "
            if dependency in addons:
                version = addons[dependency].get("version", "")
                label = f"{dependency} ({version})" if version else dependency
            else:
                label = f"{dependency} [external]"
            repeat = ""
            if dependency in seen and dependency in addons:
                repeat = "  (already shown)"
            click.secho(
                f"{prefix}{branch}{label}{repeat}",
                fg="cyan" if dependency in addons else "bright_black",
            )
            if dependency in addons and dependency not in seen:
                seen.add(dependency)
                render(dependency, prefix + ("    " if last else "|   "), seen)

    manifest = addons[module]
    version = manifest.get("version", "")
    click.secho(f"{module}" + (f"  {version}" if version else ""), bold=True)
    render(module)
    transitive = sorted(deps_mod.transitive_deps(graph, module))
    internal = [dependency for dependency in transitive if dependency in addons]
    external = [dependency for dependency in transitive if dependency not in addons]
    click.echo()
    click.echo(
        f"direct: {len(graph.get(module, []))} | transitive: {len(internal)} custom, {len(external)} external"
    )

    users = sorted(deps_mod.dependents(graph).get(module, ()))
    if users:
        click.secho(f"required by: {', '.join(users)}", fg="yellow")
    cycle = deps_mod.find_cycle(graph, module)
    if cycle:
        click.secho(f"CYCLE DETECTED: {' -> '.join(cycle)}", fg="red", bold=True)


@main.command(section="Development")
@click.argument("project")
@click.argument("db_a")
@click.argument("db_b")
@click.option("--grep", "-g", default=None, help="Filter module names.")
def diff(project, db_a, db_b, grep):
    """Compare module state and version between two databases."""
    need_docker()
    slug, project_entry = entry(project)

    def fetch(database):
        proc = compose.exec_service(
            project_entry["path"],
            "db",
            "psql",
            "-U",
            project_entry.get("db_user", "odoo"),
            "-d",
            database,
            "-At",
            "-F",
            "|",
            "-c",
            "SELECT name, state, latest_version FROM ir_module_module ORDER BY name",
        )
        return dbdiff.parse_module_states(proc.stdout.decode(errors="replace"))

    states_a, states_b = fetch(db_a), fetch(db_b)
    if grep:
        states_a = {name: value for name, value in states_a.items() if grep.lower() in name.lower()}
        states_b = {name: value for name, value in states_b.items() if grep.lower() in name.lower()}

    result = dbdiff.compare(states_a, states_b)
    total = len(result["changed"]) + len(result["only_a"]) + len(result["only_b"])
    if not total:
        click.secho(f"[{slug}] '{db_a}' and '{db_b}' are in sync.", fg="green")
        return

    width = max([len(db_a), len(db_b), 24])
    click.echo(f"{'MODULE':<40} {db_a:<{width}}  {db_b}")
    for name, (left_state, right_state) in result["changed"].items():
        left = f"{left_state[0]} {left_state[1]}"
        right = f"{right_state[0]} {right_state[1]}"
        click.echo(f"{name:<40} {left:<{width}}  {right}")
    for name in result["only_a"]:
        state = states_a[name]
        click.echo(f"{name:<40} {state[0]} {state[1]:<{width - len(state[0]) - 1}}  -")
    for name in result["only_b"]:
        state = states_b[name]
        click.echo(f"{name:<40} {'-':<{width}}  {state[0]} {state[1]}")

    click.echo()
    if result["only_a"]:
        extra = len(result["only_a"]) - 10
        suffix = f", +{extra} more" if extra > 0 else ""
        click.secho(
            f"only in {db_a} ({len(result['only_a'])}): {', '.join(result['only_a'][:10])}{suffix}",
            fg="cyan",
        )
    if result["only_b"]:
        extra = len(result["only_b"]) - 10
        suffix = f", +{extra} more" if extra > 0 else ""
        click.secho(
            f"only in {db_b} ({len(result['only_b'])}): {', '.join(result['only_b'][:10])}{suffix}",
            fg="cyan",
        )
    click.secho(f"{total} module(s) differ.", fg="yellow")


@main.command(section="Development")
@click.argument("project")
@click.argument("module", required=False)
@click.option("--db", "-d", default=None, help="Database to read install state from.")
@click.option("--grep", "-g", default=None, help="Filter module names.")
def addons(project, module, db, grep):
    """List custom addons and their database install state."""
    need_docker()
    slug, project_entry = entry(project)
    addons_dir = project_entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException(f"{slug} has no custom_addons folder registered.")
    database = pick_db(project_entry, db)

    local = list_addons(addons_dir)
    if grep:
        local = {name: manifest for name, manifest in local.items() if grep.lower() in name.lower()}
    states = {}
    try:
        names = sorted(local)
        if names:
            quoted = ",".join("'" + name.replace("'", "''") + "'" for name in names)
            sql = (
                "SELECT name, state, latest_version FROM ir_module_module "
                f"WHERE name IN ({quoted}) ORDER BY name"
            )
            proc = compose.exec_service(
                project_entry["path"],
                "db",
                "psql",
                "-U",
                project_entry.get("db_user", "odoo"),
                "-d",
                database,
                "-At",
                "-F",
                "|",
                "-c",
                sql,
            )
            for line in proc.stdout.decode().splitlines():
                parts = line.split("|")
                if parts and parts[0]:
                    states[parts[0]] = (
                        parts[1] if len(parts) > 1 else "-",
                        parts[2] if len(parts) > 2 else "-",
                    )
    except compose.DockerError as exc:
        click.secho(f"(DB state unavailable: {exc})", fg="yellow")

    if module:
        click.echo(json.dumps(local.get(module) or {}, indent=2))
        if module in states:
            click.echo(f"state: {states[module][0]}  version: {states[module][1]}")
        return

    click.echo(f"{'MODULE':<42} {'VERSION':<12} {'STATE':<12} DEPENDS")
    click.secho(f"(db: {database})", fg="cyan")
    for name, manifest in sorted(local.items()):
        version = str(manifest.get("version", "-"))
        state, db_version = states.get(name, ("-", "-"))
        dependencies = ", ".join(manifest.get("depends", []) or [])
        shown_version = db_version or version
        click.echo(f"{name:<42} {shown_version:<12} {state:<12} {dependencies[:60]}")


def resolve_changed(project_entry, since):
    addons_dir = project_entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException("Project has no custom_addons folder registered.")
    try:
        modules = vcs.changed_modules(project_entry["path"], addons_dir, ref=since)
    except vcs.GitError as exc:
        raise click.ClickException(f"git failed: {exc}")
    return addons_dir, modules


@main.command(section="Development")
@click.argument("project")
@click.argument("module", nargs=-1, required=False)
@click.option("--db", "-d", required=True, help="Database to upgrade the module(s) in.")
@click.option(
    "--changed", is_flag=True, help="Upgrade every custom addon changed vs Git HEAD (or --since REF)."
)
@click.option("--since", default=None, help="Git ref to diff against (with --changed).")
@click.option("--keep-stopped", is_flag=True, help="Leave web stopped afterwards.")
def upgrade(project, module, db, changed, since, keep_stopped):
    """Upgrade one or more addons, restarting web around the operation."""
    need_docker()
    slug, project_entry = entry(project)
    if changed and module:
        raise click.ClickException("Pass either MODULE(s) or --changed, not both.")
    if changed:
        _, modules = resolve_changed(project_entry, since)
        if not modules:
            raise click.ClickException(
                f"No changed custom addons detected under {project_entry.get('custom_addons')}."
            )
    elif module:
        modules = list(module)
    else:
        raise click.ClickException("Pass a module name or --changed.")

    click.secho(f"[{slug}] upgrading: {', '.join(modules)}", fg="cyan")
    web = project_entry["services"]["web"]
    was_running = compose.web_running(project_entry["path"], project_entry)
    if was_running:
        click.echo(f"[{slug}] stopping web...")
        compose.run(project_entry["path"], "stop", web)
    command = [
        "odoo",
        "-c",
        ODOO_CONF,
        "-d",
        db,
        "-u",
        ",".join(modules),
        "--stop-after-init",
    ]
    return_code = compose.live_output(project_entry["path"], "run", "--rm", web, *command)
    if was_running and not keep_stopped:
        click.echo(f"[{slug}] starting web back...")
        compose.run(project_entry["path"], "start", web)
    if return_code != 0:
        raise click.ClickException(f"[{slug}] upgrade failed (rc={return_code}).")
    click.secho(f"[{slug}] upgraded {', '.join(modules)} in {db}.", fg="green")


@main.command(section="Development")
@click.argument("project")
@click.argument("module", required=False)
@click.option("--all", "all_", is_flag=True, help="Test every custom addon in its own throwaway database.")
@click.option("--changed", is_flag=True, help="Test every custom addon changed vs Git HEAD (or --since REF).")
@click.option("--since", default=None, help="Git ref to diff against (with --changed).")
@click.option("-x", "--stop-on-fail", is_flag=True, help="With --all/--changed: stop at the first failure.")
@click.option("--db", "-d", default=None, help="Test DB name (default: test_<module>).")
@click.option("--keep-db", is_flag=True, help="Keep the throwaway test database after the run.")
@click.option("--test-tags", "-t", default=None, help="Override --test-tags (default: /<module>).")
@click.option("--timeout", type=int, default=None, help="Seconds before giving up.")
def test(project, module, all_, changed, since, stop_on_fail, db, keep_db, test_tags, timeout):
    """Run addon tests in disposable databases."""
    need_docker()
    slug, project_entry = entry(project)
    addons_dir = project_entry.get("custom_addons")
    if not addons_dir:
        raise click.ClickException(f"{slug} has no custom_addons folder registered.")

    selected = [flag for flag, enabled in (("--all", all_), ("--changed", changed)) if enabled]
    if len(selected) > 1 or (selected and module):
        raise click.ClickException("Pass only one of: MODULE, --all, --changed.")
    if not module and not selected:
        raise click.ClickException("Pass a module name, --all or --changed (see odooctl test --help).")

    multiple = False
    if all_:
        manifests = list_addons(addons_dir)
        modules = [name for name, manifest in sorted(manifests.items()) if manifest.get("installable", True)]
        skipped = sorted(set(manifests) - set(modules))
        if not modules:
            raise click.ClickException(f"No installable addons found in {addons_dir}")
        suffix = f" (skipping not-installable: {', '.join(skipped)})" if skipped else ""
        click.echo(f"[{slug}] testing {len(modules)} module(s){suffix}")
        multiple = True
    elif changed:
        _, modules = resolve_changed(project_entry, since)
        if not modules:
            raise click.ClickException(f"No changed custom addons detected under {addons_dir}.")
        click.echo(f"[{slug}] testing {len(modules)} changed module(s): {', '.join(modules)}")
        multiple = True
    else:
        if not (Path(addons_dir) / module / "__manifest__.py").exists():
            raise click.ClickException(f"Module '{module}' not found in {addons_dir}")
        modules = [module]

    results = []
    for index, current_module in enumerate(modules, 1):
        safe_name = "".join(
            character if character.isalnum() or character == "_" else "_" for character in current_module
        )[:40]
        database = db if (db and not multiple) else f"test_{safe_name}"
        label = f"({index}/{len(modules)})" if multiple else ""
        click.echo(f"[{slug}] {label} running tests for {current_module} in throwaway DB '{database}'...")
        result = testing.run_tests(
            project_entry["path"],
            project_entry,
            current_module,
            database,
            test_tags=test_tags,
            keep_db=keep_db,
            timeout=timeout,
        )
        results.append((current_module, result))
        if stop_on_fail and not result.ok:
            click.secho(f"[{slug}] stopping on first failure.", fg="yellow")
            break

    if len(results) == 1 and not multiple:
        _, result = results[0]
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
        failed = [(name, result) for name, result in results if not result.ok]
        passed = [(name, result) for name, result in results if result.ok]
        click.echo(f"\n{'MODULE':<40} {'RESULT':<8} TESTS")
        for name, result in passed:
            click.secho(f"{name:<40} {'PASS':<8} {result.ran if result.ran else '-'}", fg="green")
        for name, result in failed:
            click.secho(f"{name:<40} {'FAIL':<8} {result.ran if result.ran else '-'}", fg="red")
        click.echo()
        not_run = f", {len(modules) - len(results)} not run" if stop_on_fail else ""
        click.secho(f"{len(passed)} passed, {len(failed)} failed{not_run}", bold=True)

    last_result = results[-1][1]
    if last_result.log_path:
        click.echo(f"log: {last_result.log_path}")
    sys.exit(0 if all(result.ok for _, result in results) else 1)
