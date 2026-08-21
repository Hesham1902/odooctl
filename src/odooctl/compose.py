import json
import re
import subprocess
from pathlib import Path

import yaml


class DockerError(RuntimeError):
    pass


def _base(project_path: Path):
    compose = Path(project_path) / "docker-compose.yml"
    if not compose.is_file():
        raise DockerError(f"No docker-compose.yml in {project_path}")
    return ["docker", "compose", "-f", str(compose), "--project-directory", str(project_path)]


def run(project_path, *args, capture=True, input_bytes=None, check=True, timeout=None,
        stdout_file=None, stdin_file=None):
    cmd = _base(project_path) + list(args)
    try:
        if stdout_file is not None or stdin_file is not None:
            proc = subprocess.run(
                cmd,
                stdout=stdout_file or subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=stdin_file,
                timeout=timeout,
            )
        else:
            proc = subprocess.run(
                cmd,
                capture_output=capture,
                input=input_bytes,
                timeout=timeout,
            )
    except FileNotFoundError:
        raise DockerError("Docker not found. Is Docker Desktop installed and on PATH?")
    if check and proc.returncode != 0:
        stderr = (proc.stderr or b"").decode(errors="replace").strip()
        stdout = (proc.stdout or b"").decode(errors="replace").strip()
        raise DockerError(stderr or stdout or f"docker compose {args[0]} failed (rc={proc.returncode})")
    return proc


def live_output(project_path, *args):
    cmd = _base(project_path) + list(args)
    proc = subprocess.Popen(cmd)
    return proc.wait()


def daemon_available():
    try:
        proc = subprocess.run(["docker", "info", "--format", "ok"], capture_output=True, timeout=15)
        return proc.returncode == 0
    except Exception:
        return False


def ps(project_path):
    proc = run(project_path, "ps", "--format", "json")
    rows = []
    for line in (proc.stdout or b"").decode().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def service_state(project_path, service):
    for row in ps(project_path):
        if row.get("Service") == service:
            return (row.get("State") or "").lower(), row
    return None, None


def exec_service(project_path, service, *cmd, capture=True, input_bytes=None, user=None, check=True):
    args = ["exec", "-T"]
    if user:
        args += ["-u", user]
    args += [service] + list(cmd)
    return run(project_path, *args, capture=capture, input_bytes=input_bytes, check=check)


def databases(project_path, db_user="odoo"):
    state, _ = service_state(project_path, "db")
    if state != "running":
        return None
    proc = exec_service(
        project_path,
        "db",
        "psql",
        "-U",
        db_user,
        "-d",
        "postgres",
        "-Atc",
        "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY datname",
    )
    return [line for line in proc.stdout.decode().splitlines() if line.strip()]


def project_image_name(project_path, service):
    base = Path(project_path).resolve().name.lower()
    base = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-")
    return f"{base}-{service}"


def local_image(name):
    proc = subprocess.run(
        ["docker", "image", "inspect", name, "--format", "{{.Id}}"],
        capture_output=True,
    )
    return name if proc.returncode == 0 else None


def find_built_image(entry, role):
    candidates = []
    explicit = (entry.get("images") or {}).get(role)
    if explicit:
        candidates.append(explicit)
    candidates.append(project_image_name(entry["path"], entry["services"][role]))
    try:
        data = yaml.safe_load(Path(entry["compose_file"]).read_text()) or {}
        top_name = data.get("name")
        if top_name:
            base = re.sub(r"[^a-z0-9_-]+", "-", str(top_name).lower()).strip("-")
            candidates.append(f"{base}-{entry['services'][role]}")
    except OSError:
        pass
    for candidate in candidates:
        found = local_image(candidate)
        if found:
            return found
    return None


def web_running(project_path, entry):
    state, _ = service_state(project_path, entry["services"]["web"])
    return state == "running"
