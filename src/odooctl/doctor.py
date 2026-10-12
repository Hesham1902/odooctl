"""Read-only checks for problems that prevent common odooctl operations."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import __version__, compose, provision, registry, update


@dataclass(frozen=True)
class Check:
    """One observed condition and the next step when it needs attention."""

    status: str
    name: str
    detail: str
    fix: str = ""


def _run(command):
    """Return a short command result without allowing a missing tool to abort doctor."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return False, "command not found"
    except subprocess.TimeoutExpired:
        return False, "timed out after 5 seconds"
    except OSError as exc:
        return False, str(exc)
    output = (result.stdout or "").strip() or (result.stderr or "").strip()
    return result.returncode == 0, output.splitlines()[0][:400] if output else f"exit {result.returncode}"


def _docker_checks():
    """Verify Docker CLI, Compose v2, and daemon access independently."""
    if not shutil.which("docker"):
        return [
            Check(
                "FAIL",
                "Docker",
                "docker is not on PATH",
                "Install Docker Desktop or OrbStack and reopen your terminal.",
            ),
        ], False

    checks = [Check("PASS", "Docker", "docker command found")]
    compose_ok, detail = _run(["docker", "compose", "version"])
    checks.append(
        Check(
            "PASS" if compose_ok else "FAIL",
            "Compose v2",
            detail,
            "" if compose_ok else "Install or enable Docker Compose v2; run `docker compose version`.",
        )
    )
    daemon_ok, detail = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    checks.append(
        Check(
            "PASS" if daemon_ok else "FAIL",
            "Docker daemon",
            detail,
            (
                ""
                if daemon_ok
                else "Start Docker Desktop or OrbStack; if it is running, check "
                "`docker context ls` and `docker info`."
            ),
        )
    )
    return checks, compose_ok


def _project_checks(compose_available):
    """Validate saved paths and Compose syntax without scanning or changing projects."""
    try:
        config = registry.load_config()
    except (OSError, ValueError) as exc:
        return [
            Check(
                "FAIL",
                "Project registry",
                f"Cannot read {registry._config_file()}: {exc}",
                "Repair the config JSON, or move it aside and run `odooctl discover`.",
            )
        ], {}
    if not isinstance(config, dict) or not isinstance(config.get("projects", {}), dict):
        return [
            Check(
                "FAIL",
                "Project registry",
                "Config must contain a projects object.",
                "Repair the config JSON, or move it aside and run `odooctl discover`.",
            )
        ], {}

    projects = config.get("projects") or {}
    if not projects:
        return [
            Check("WARN", "Projects", "No projects registered.", "Run `odooctl discover --root PATH`.")
        ], config

    checks = []
    for slug, entry in sorted(projects.items()):
        if not isinstance(entry, dict):
            checks.append(
                Check("FAIL", f"Project {slug}", "Invalid registry entry.", "Run `odooctl discover`.")
            )
            continue
        path_value = entry.get("path")
        project_path = Path(path_value) if isinstance(path_value, str) and path_value else None
        if project_path is None or not project_path.is_dir():
            checks.append(
                Check(
                    "FAIL",
                    f"Project {slug}",
                    f"Folder missing: {path_value or '(unset)'}",
                    "Run `odooctl discover` to refresh registered projects.",
                )
            )
            continue
        canonical = provision.canonical_path(project_path)
        if str(canonical) != str(project_path):
            checks.append(
                Check(
                    "FAIL",
                    f"Project {slug}",
                    f"Registered path {project_path} differs in letter case from disk ({canonical}); "
                    "Docker shares only the on-disk spelling, so bind mounts come up empty.",
                    f"Run `odooctl discover --root {canonical.parent}` to re-register it, "
                    f"then `odooctl down {slug}` and `odooctl up {slug}`.",
                )
            )
            continue
        compose_file = compose.find_compose_file(project_path)
        if compose_file is None:
            checks.append(
                Check(
                    "FAIL",
                    f"Project {slug}",
                    f"No Compose file in {project_path}",
                    "Restore the Compose file or run `odooctl discover` to refresh the registry.",
                )
            )
            continue
        if not compose_available:
            checks.append(Check("WARN", f"Project {slug}", "Compose file found; validation skipped."))
            continue
        try:
            result = compose.run(project_path, "config", "--quiet", check=False, timeout=5)
        except (compose.DockerError, OSError, subprocess.TimeoutExpired) as exc:
            checks.append(
                Check(
                    "FAIL",
                    f"Project {slug}",
                    str(exc)[:400],
                    f"Fix {compose_file}, then rerun `odooctl doctor`.",
                )
            )
            continue
        if result.returncode:
            detail = (
                (result.stderr or result.stdout or b"Compose config failed")
                .decode(errors="replace")
                .strip()
                .splitlines()[0][:400]
            )
            checks.append(
                Check(
                    "FAIL",
                    f"Project {slug}",
                    detail,
                    f"Fix {compose_file}, then rerun `odooctl doctor`.",
                )
            )
        else:
            checks.append(Check("PASS", f"Project {slug}", f"Compose config valid: {compose_file}"))
    return checks, config


def _ssh_check(config):
    """Treat missing SSH clients as blocking only when pull settings are saved."""
    missing = [tool for tool in ("ssh", "scp") if not shutil.which(tool)]
    if not missing:
        return Check("PASS", "SSH tools", "ssh and scp found")
    needed = bool(config.get("pull"))
    return Check(
        "FAIL" if needed else "WARN",
        "SSH tools",
        f"Missing: {', '.join(missing)}" + ("; saved pull connections need them." if needed else "."),
        "Install OpenSSH to use `odooctl pull`.",
    )


def _update_check():
    """Probe the published release feed directly and expose connection failures."""
    if os.environ.get("ODOOCTL_NO_UPDATE_CHECK", "").lower() in {"1", "true", "yes", "on"}:
        return Check(
            "WARN",
            "Updates",
            "Automatic update checks are disabled.",
            "Unset ODOOCTL_NO_UPDATE_CHECK to receive release notices.",
        )
    try:
        latest = update._fetch_latest_version()
    except Exception as exc:
        return Check(
            "FAIL",
            "Updates",
            f"Cannot reach the release feed: {exc}",
            f"Check HTTPS access to {update.RELEASE_API_URL}.",
        )
    current = update._version_tuple(__version__)
    remote = update._version_tuple(latest)
    if remote is None:
        return Check("FAIL", "Updates", "Release feed returned an invalid version.")
    if current is not None and remote > current:
        return Check(
            "WARN",
            "Updates",
            f"odooctl {latest} is available; installed: {__version__}.",
            f"Download it from {update.release_url(latest)}.",
        )
    return Check("PASS", "Updates", f"Release feed reachable; installed: {__version__}, latest: {latest}.")


def run_checks():
    """Check prerequisites, saved projects, optional SSH, and release reachability."""
    docker_checks, compose_available = _docker_checks()
    project_checks, config = _project_checks(compose_available)
    return [*docker_checks, *project_checks, _ssh_check(config), _update_check()]
