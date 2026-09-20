"""Shared pull-and-restore workflow for the CLI and desktop application."""

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import admin, compose, icons, pull, registry, restore, sanitize

Progress = Callable[[str], None]


class PullWorkflowError(RuntimeError):
    """A pull could not be planned or completed."""


class ExistingDatabaseError(PullWorkflowError):
    """The selected target database exists and overwrite was not authorized."""

    def __init__(self, database):
        self.database = database
        super().__init__(
            f"Database '{database}' already exists. Enable 'Replace existing database' "
            "or confirm the overwrite from the CLI."
        )


@dataclass(frozen=True)
class PullOptions:
    """User choices that control one pull and restore operation."""

    ssh_target: str | None = None
    remote_path: str | None = None
    database: str | None = None
    ssh_key: str | None = None
    with_filestore: bool = False
    reset_admin: bool = True
    fix_icons: bool = True
    sanitize: bool = True
    keep_download: bool = False
    overwrite: bool = False
    save_settings: bool = False


@dataclass(frozen=True)
class PullPlan:
    """Resolved connection and target values used by a pull."""

    slug: str
    ssh_target: str
    port: int | None
    remote_path: str | None
    ssh_key: str | None
    database: str
    options: PullOptions


@dataclass(frozen=True)
class PullResult:
    """Outcome and user-facing log from a pull."""

    slug: str
    database: str
    filestore: bool
    messages: tuple[str, ...]

    @property
    def output(self):
        """Return the workflow log as displayable text."""
        return "\n".join(self.messages)


def plan_pull(slug, options):
    """Resolve saved settings and validate the connection inputs."""
    saved = registry.load_pull_settings(slug)
    ssh_target = options.ssh_target or saved.get("from")
    if not ssh_target:
        raise PullWorkflowError(
            "Pass --from ssh://user@host the first time, for example "
            "ssh://1234567@acme.odoo.com. Hint: add --save to remember it for future pulls."
        )
    remote_path = options.remote_path or saved.get("path")
    ssh_key = options.ssh_key or saved.get("key")
    if ssh_key:
        ssh_key = str(Path(ssh_key).expanduser())
        if not Path(ssh_key).is_file():
            raise PullWorkflowError(f"SSH key file does not exist: {ssh_key}")
    database = options.database or saved.get("db") or f"{slug}_pulled"
    try:
        target, port = pull.parse_target(ssh_target)
    except pull.PullError as exc:
        raise PullWorkflowError(str(exc)) from exc
    return PullPlan(slug, target, port, remote_path, ssh_key, database, options)


def check_database_ready(project_entry, database):
    """Require a running database service and return existing database names."""
    state, _ = compose.service_state(project_entry["path"], project_entry["services"]["db"])
    if state != "running":
        raise PullWorkflowError(
            "db container is not running. Start the project before pulling a backup."
        )
    return compose.databases(project_entry["path"], project_entry.get("db_user", "odoo")) or []


def run_pull(slug, project_entry, options, progress=print, plan=None):
    """Download, restore, and optionally harden an Odoo.sh backup."""
    plan = plan or plan_pull(slug, options)
    messages = []

    def emit(message):
        text = str(message).replace("\r", "")
        if text:
            messages.append(text)
            progress(text)

    if options.save_settings:
        registry.save_pull_settings(
            slug,
            {
                "from": options.ssh_target or plan.ssh_target,
                "path": options.remote_path or plan.remote_path,
                "key": plan.ssh_key,
                "db": options.database or plan.database,
            },
        )
        emit(f"[{slug}] saved pull settings (next time: odooctl pull {slug}).")

    existing = check_database_ready(project_entry, plan.database)
    if plan.database in existing and not options.overwrite:
        raise ExistingDatabaseError(plan.database)
    if not options.database:
        emit(f"(no database given; restoring as '{plan.database}')")

    emit(f"[{slug}] looking for the latest backup on {plan.ssh_target}...")
    try:
        remote = pull.find_remote_backup(
            plan.ssh_target,
            port=plan.port,
            path=plan.remote_path,
            key=plan.ssh_key,
        )
        filestore_note = " (filestore available)" if remote.get("mirror") else " (dump only)"
        emit(f"[{slug}] found {remote['sql_gz']}{filestore_note}")
        if not options.with_filestore:
            emit(f"[{slug}] skipping filestore (enable 'Include filestore' to include attachments)")
        download_kwargs = {
            "key": plan.ssh_key,
            "with_filestore": options.with_filestore,
        }
        if progress is not print:
            download_kwargs["progress"] = emit
        local = pull.download(
            plan.ssh_target,
            plan.port,
            remote,
            Path(project_entry["path"]) / "backups" / "pulled",
            **download_kwargs,
        )
    except pull.PullError as exc:
        raise PullWorkflowError(str(exc)) from exc

    size_mb = sum(item.stat().st_size for item in local.rglob("*") if item.is_file()) / 1e6
    emit(f"[{slug}] downloaded {local.name}/ ({size_mb:.1f} MB)")
    if local.is_dir():
        dumps = sorted(local.glob("*.sql.gz"))
        source_db = restore._dump_create_target(dumps[0]) if dumps else None
        if source_db:
            emit(f"[{slug}] source database in dump: '{source_db}' -> will become '{plan.database}'")

    was_running = compose.web_running(project_entry["path"], project_entry)
    if was_running:
        emit(f"[{slug}] stopping web for the restore...")
        compose.run(project_entry["path"], "stop", project_entry["services"]["web"])

    try:
        emit(f"[{slug}] restoring into '{plan.database}'...")
        try:
            info = restore.restore(project_entry["path"], project_entry, local, plan.database)
        except (ValueError, compose.DockerError) as exc:
            raise PullWorkflowError(f"restore failed: {exc}") from exc

        if options.with_filestore and not info.get("filestore"):
            emit("[!] no filestore found remotely - attachments missing")

        for extension in info.get("skipped_extensions") or []:
            emit(
                f"[!] postgres extension '{extension}' not available locally - skipped "
                "(install it in your db image if you need it)"
            )

        if not info.get("filestore") and options.fix_icons:
            emit(f"[{slug}] no filestore - re-importing menu icons from addon sources...")
            try:
                counts = icons.fix_icons(project_entry["path"], project_entry, plan.database)
                emit(
                    f"[{slug}] menu icons: checked {counts.get('checked', 0)}, "
                    f"re-imported {counts.get('fixed', 0)}."
                )
            except (compose.DockerError, RuntimeError) as exc:
                emit(f"[!] icon repair failed: {exc}")
        elif not info.get("filestore"):
            emit("[!] menu icon repair skipped (the option is disabled)")

        if options.reset_admin:
            try:
                result = admin.reset_admin(project_entry["path"], project_entry, plan.database)
                emit(
                    f"[{slug}] login ready: admin / admin  "
                    f"(user #{result['id']}, was '{result['old_login']}')"
                )
            except (compose.DockerError, RuntimeError) as exc:
                emit(f"[!] reset-admin failed: {exc}")
        else:
            emit(f"[{slug}] admin reset skipped; restored credentials were kept.")

        if options.sanitize:
            emit(f"[{slug}] sanitizing (neutralizing) '{plan.database}'...")
            try:
                counts = sanitize.sanitize(project_entry["path"], project_entry, plan.database)
                for key, label in sanitize.LABELS:
                    if counts.get(key):
                        emit(f"[{slug}] {counts[key]:>6}  {label}")
            except (compose.DockerError, RuntimeError) as exc:
                emit(f"[!] sanitize failed: {exc}")
        else:
            emit(f"[{slug}] sanitization skipped; the restored database was not neutralized.")
    finally:
        if was_running:
            emit(f"[{slug}] starting web back...")
            compose.run(project_entry["path"], "start", project_entry["services"]["web"])

    if options.keep_download:
        emit(f"[{slug}] bundle kept at {local}")
    else:
        shutil.rmtree(local, ignore_errors=True)
        emit(f"[{slug}] cleaned up download.")

    http_port = project_entry.get("ports", {}).get("http")
    if http_port:
        emit(f"[{slug}] done -> http://localhost:{http_port}")
    return PullResult(slug, plan.database, bool(info.get("filestore")), tuple(messages))
