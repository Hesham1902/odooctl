"""Onboarding helpers for `odooctl init`.

The low-level provisioning primitives (`rewrite_compose`, `pick_template`,
`init_project`, ...) stay in :mod:`odooctl.provision`, unit-tested in
isolation. This module owns everything around *deciding what to do* and
*talking to the user about it*, kept independent of Click so it is easy to
test directly:

- parsing/validating addon-repository options (from flags or the wizard)
- preflight checks that catch mistakes before anything is created
  (backup readability, git availability, ...)
- cloning addon repositories into a project's ``custom_addons/``
- turning terse ``RuntimeError`` messages from `provision.py` into
  human-readable errors with a hint and a "nothing changed" reassurance
"""

import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import provision
from . import restore as restore_mod


class InitError(RuntimeError):
    """A preflight/planning failure for `init`.

    Carries an optional actionable hint and whether anything was already
    changed on disk/registry/Docker when it was raised (true for every
    preflight check, since those all run before any mutation).
    """

    def __init__(self, message, hint=None, nothing_changed=True):
        self.message = message
        self.hint = hint
        self.nothing_changed = nothing_changed
        super().__init__(self._render())

    def _render(self):
        parts = [self.message]
        if self.hint:
            parts.append(f"Hint: {self.hint}")
        if self.nothing_changed:
            parts.append("Nothing was changed.")
        return "\n".join(parts)

    def __str__(self):
        return self._render()


def stdin_is_interactive():
    """True when stdin is a real terminal we can safely prompt on."""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Addon repositories
# ---------------------------------------------------------------------------


@dataclass
class AddonRepoSpec:
    url: str
    ref: str = None
    name: str = ""


@dataclass
class AddonRepoResult:
    spec: AddonRepoSpec
    status: str  # planned | ok | nested | empty | conflict | error
    path: object
    modules: list = field(default_factory=list)
    message: str = ""


def derive_repo_name(url):
    """Best-effort folder name for a repo URL: last path segment, no .git."""
    tail = url.rstrip("/").rstrip(":").split("/")[-1]
    tail = tail.split(":")[-1]  # scp-style git@host:group/repo -> repo
    if tail.endswith(".git"):
        tail = tail[:-4]
    slug = provision.slugify(tail)
    return slug or "addon-repo"


def parse_addon_repo_specs(raw_values):
    """Turn repeatable --addon-repo strings ("URL" or "URL#ref") into specs.

    Names are derived from the URL and de-duplicated (repo, repo-2, repo-3, ...)
    so two repos never target the same custom_addons/ subfolder.
    """
    specs = []
    used = set()
    for raw in raw_values or ():
        raw = (raw or "").strip()
        if not raw:
            continue
        url, _, ref = raw.partition("#")
        url = url.strip()
        ref = ref.strip() or None
        if not url:
            raise InitError(
                f"Invalid --addon-repo value {raw!r}: missing a repository URL.",
                hint="Use URL or URL#branch, e.g. https://github.com/OCA/queue.git#18.0",
            )
        base = derive_repo_name(url)
        name = base
        i = 2
        while name in used:
            name = f"{base}-{i}"
            i += 1
        used.add(name)
        specs.append(AddonRepoSpec(url=url, ref=ref, name=name))
    return specs


def _detect_modules(dest: Path):
    """Odoo module folder names visible to Odoo if `dest` is dropped straight
    into custom_addons/: either `dest` itself is a module, or nothing is
    (any modules found one level deeper are reported as `nested`, not `ok`,
    since custom_addons/ only scans its direct children)."""
    if (dest / "__manifest__.py").is_file():
        return ["."]
    try:
        children = sorted(p.name for p in dest.iterdir() if p.is_dir())
    except OSError:
        return []
    return [c for c in children if (dest / c / "__manifest__.py").is_file()]


def clone_addon_repos(custom_addons_dir, specs, dry_run=False):
    """Clone each addon repo spec into custom_addons_dir.

    Never overwrites an existing directory. Never runs anything from inside
    the cloned repo (no setup scripts, no `pip install`). Returns one
    AddonRepoResult per spec, always - clone failures are reported, not
    raised, so one bad repo does not abort the rest of `init`.
    """
    results = []
    custom_addons_dir = Path(custom_addons_dir)
    direct_root = False
    if not dry_run and len(specs) == 1:
        try:
            direct_root = not any(custom_addons_dir.iterdir())
        except OSError:
            direct_root = False
    for spec in specs:
        dest = custom_addons_dir if direct_root else custom_addons_dir / spec.name
        if dry_run:
            target = "custom_addons/ directly" if len(specs) == 1 else f"custom_addons/{spec.name}/"
            results.append(AddonRepoResult(spec, "planned", dest, [], f"would clone {spec.url} -> {target}"))
            continue
        if not direct_root and dest.exists():
            results.append(
                AddonRepoResult(
                    spec, "conflict", dest, [], f"{dest} already exists - left untouched, skipped clone"
                )
            )
            continue
        if shutil.which("git") is None:
            results.append(
                AddonRepoResult(
                    spec, "error", None, [], "git not found on PATH; install git and clone this repo manually"
                )
            )
            continue
        clone_dest = dest
        if direct_root:
            clone_dest = custom_addons_dir / f".{spec.name}.clone"
        cmd = ["git", "clone", "--quiet"]
        if spec.ref:
            cmd += ["--branch", spec.ref, "--single-branch"]
        cmd += [spec.url, str(clone_dest)]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode(errors="replace").strip()
            shutil.rmtree(clone_dest, ignore_errors=True)
            results.append(
                AddonRepoResult(
                    spec,
                    "error",
                    None,
                    [],
                    stderr or f"git clone failed for {spec.url} (exit {proc.returncode})",
                )
            )
            continue
        result = _result_for_clone(spec, clone_dest)
        if direct_root and result.status == "nested":
            try:
                for child in clone_dest.iterdir():
                    shutil.move(str(child), str(custom_addons_dir / child.name))
                clone_dest.rmdir()
            except OSError as exc:
                shutil.rmtree(clone_dest, ignore_errors=True)
                result.status = "error"
                result.message = f"could not place repository directly in custom_addons/: {exc}"
            else:
                result.status = "direct"
                result.path = custom_addons_dir
                result.message = (
                    f"collection repo '{spec.name}' ready directly in custom_addons/: "
                    f"{len(result.modules)} module(s) detected"
                )
        elif direct_root:
            final = custom_addons_dir / spec.name
            try:
                shutil.move(str(clone_dest), str(final))
            except OSError as exc:
                shutil.rmtree(clone_dest, ignore_errors=True)
                result.status = "error"
                result.message = f"could not place repository in custom_addons/{spec.name}: {exc}"
            else:
                result.path = final
        results.append(result)
    return results


def _result_for_clone(spec, dest):
    modules = _detect_modules(dest)
    if not modules:
        return AddonRepoResult(
            spec, "empty", dest, [], f"cloned but no __manifest__.py found under custom_addons/{spec.name}/"
        )
    if modules == ["."]:
        message = f"module '{spec.name}' ready"
        if (dest / "requirements.txt").is_file():
            message += _requirements_note(spec.name)
        return AddonRepoResult(spec, "ok", dest, [spec.name], message)

    more = f" (+{len(modules) - 5} more)" if len(modules) > 5 else ""
    message = (
        f"custom_addons/{spec.name}/ bundles {len(modules)} module(s) "
        f"({', '.join(modules[:5])}{more}) nested one level deep. The repository "
        "must be exposed through Odoo's addons_path so all modules are visible."
    )
    if (dest / "requirements.txt").is_file():
        message += _requirements_note(spec.name)
    return AddonRepoResult(spec, "nested", dest, modules, message)


def _requirements_note(name):
    return (
        f"\n      note: {name}/requirements.txt found - Python deps aren't installed automatically. "
        "Add them to odoo.Dockerfile and rebuild (`odooctl init --build` next time, or "
        "`docker compose build` in the project) if the module needs them."
    )


def _custom_addons_container_path(project_entry):
    """Find the container path backed by the project's custom_addons folder."""
    compose_path = Path(project_entry["compose_file"])
    try:
        data = yaml.safe_load(compose_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None
    host_root = Path(project_entry.get("custom_addons", compose_path.parent / "custom_addons")).resolve()
    for service in (data.get("services") or {}).values():
        for volume in service.get("volumes") or []:
            if isinstance(volume, str):
                parts = volume.split(":")
                if len(parts) < 2:
                    continue
                if len(parts) >= 3 and len(parts[0]) == 1 and parts[0].isalpha():
                    source, target = ":".join(parts[:2]), parts[2]
                else:
                    source, target = parts[0], parts[1]
                source_path = Path(source)
                if not source_path.is_absolute():
                    source_path = compose_path.parent / source_path
                if source_path.resolve() == host_root:
                    return target.rstrip("/") or "/"
            elif isinstance(volume, dict):
                source = volume.get("source")
                target = volume.get("target")
                if not source or not target:
                    continue
                source_path = Path(source)
                if not source_path.is_absolute():
                    source_path = compose_path.parent / source_path
                if source_path.resolve() == host_root:
                    return str(target).rstrip("/") or "/"
    return None


def configure_nested_addon_paths(project_entry, results):
    """Expose collection-repository modules through Odoo's configured addons path.

    Collection repositories remain nested so their Git metadata stays intact. Odoo
    can scan each nested repository when its directory is added to addons_path.
    """
    nested = [result for result in results if result.status == "nested"]
    if not nested:
        return results
    config_path = Path(project_entry["path"]) / "config" / "odoo.conf"
    container_root = _custom_addons_container_path(project_entry)
    if not config_path.is_file() or not container_root:
        for result in nested:
            result.status = "error"
            result.message += (
                "\n      could not configure Odoo automatically: the custom_addons bind mount or "
                "config/odoo.conf was not found. Add the repository directory to addons_path manually."
            )
        return results

    try:
        text = config_path.read_text()
    except OSError as exc:
        for result in nested:
            result.status = "error"
            result.message += f"\n      could not read {config_path}: {exc}"
        return results

    lines = text.splitlines(keepends=True)
    line_index = next(
        (index for index, line in enumerate(lines) if line.lstrip().startswith("addons_path") and "=" in line),
        None,
    )
    if line_index is None:
        for result in nested:
            result.status = "error"
            result.message += (
                f"\n      {config_path} has no addons_path setting; add one containing the repository directory."
            )
        return results

    line = lines[line_index]
    prefix, current = line.split("=", 1)
    newline = "\n" if line.endswith("\n") else ""
    configured = [entry.strip() for entry in current.strip().split(",") if entry.strip()]
    added = []
    for result in nested:
        path = f"{container_root}/{result.spec.name}".replace("//", "/")
        if path not in configured:
            configured.append(path)
            added.append(path)
        result.status = "configured"
        result.message = (
            f"collection repo '{result.spec.name}' ready: {len(result.modules)} module(s) visible via {path}"
        )
    if added:
        lines[line_index] = f"{prefix}= {', '.join(configured)}{newline}"
        try:
            config_path.write_text("".join(lines))
        except OSError as exc:
            for result in nested:
                result.status = "error"
                result.message = f"could not update {config_path}: {exc}"
    return results


# ---------------------------------------------------------------------------
# Backup validation
# ---------------------------------------------------------------------------


def validate_backup(path: Path):
    """Cheap, local, read-only checks that a backup is usable. Returns the
    detected format string, or raises InitError explaining what's wrong."""
    if not path.exists():
        raise InitError(f"Backup path not found: {path}", hint="Check the path passed to --from.")
    fmt = restore_mod.detect_format(path)
    if not fmt:
        raise InitError(
            f"Unrecognized backup format: {path}",
            hint="Expected a .zip (Odoo.sh export), a .dump/.backup (pg_dump -Fc), or an odooctl backup folder.",
        )
    if fmt == "zip":
        if not zipfile.is_zipfile(path):
            raise InitError(
                f"{path} has a .zip extension but isn't a valid zip archive.",
                hint="Re-download the backup; it may be truncated or corrupted.",
            )
    elif fmt == "dump":
        try:
            header = path.read_bytes()[:5]
        except OSError as exc:
            raise InitError(f"Cannot read backup file {path}: {exc}") from exc
        if header != b"PGDMP":
            raise InitError(
                f"{path} doesn't look like a pg_dump custom-format file (no PGDMP header).",
                hint="Export with `pg_dump -Fc ...`, or point --from at the right file.",
            )
    elif fmt == "odooctl_dir":
        if not (path / "db.dump").exists():
            raise InitError(f"{path} is missing db.dump.", hint="Point --from at a valid odooctl backup folder.")
    elif fmt == "odoosh_raw":
        if not list(path.glob("*.sql.gz")):
            raise InitError(
                f"{path} has no *.sql.gz files.", hint="Point --from at the extracted Odoo.sh backup folder."
            )
    return fmt


# ---------------------------------------------------------------------------
# Friendly error wrapping for provision.py's RuntimeErrors
# ---------------------------------------------------------------------------

_HINTS = (
    ("No registered projects to use as template", "Run `odooctl discover` to register at least one project first."),
    ("Cannot derive a folder name", "Pick a name with at least one letter or digit."),
    (
        "Unknown template",
        "Run `odooctl projects` to see registered slugs, or drop --template to auto-pick by version.",
    ),
    (
        "Need --version or --template",
        "Pass --version 18 (or similar) or --template SLUG, or run `odooctl init` with no name for "
        "the guided setup.",
    ),
    (
        "No registered project on Odoo",
        "Run `odooctl discover --root /path` to register one on that version, or pick a different --version.",
    ),
    (
        "already exists.",
        "Choose a different name or --parent-dir, or remove the old one with "
        "`odooctl remove SLUG --purge-folder`.",
    ),
    ("No free port found", "Free up local ports or stop unused projects with `odooctl down SLUG`, then retry."),
    (
        "does not define both a web and a db service",
        "Pick a different --template - run `odooctl projects` to see what's registered.",
    ),
)


def wrap_provision_error(exc: RuntimeError) -> InitError:
    message = str(exc)
    hint = next((h for needle, h in _HINTS if needle in message), None)
    return InitError(message, hint=hint)
