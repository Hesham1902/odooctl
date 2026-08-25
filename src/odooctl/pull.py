import shutil
import subprocess
import time
from pathlib import Path

ODOO_SH_BACKUP_DIRS = [
    "~/backup.daily",
    "~/backup.weekly",
    "~/backup.monthly",
    "/backup.daily",
    "/backup.weekly",
    "/backup.monthly",
]


class PullError(RuntimeError):
    pass


def _fmt_mb(n):
    return f"{n / 1e6:.1f} MB"


class _ProgressPipe:
    """Pass bytes stdin->dst while printing a running total on one line."""

    def __init__(self, label="downloading"):
        self.label = label
        self.total = 0
        self._start = time.monotonic()
        self._last_render = 0.0

    def pump(self, src, dst, chunk=1 << 20):
        while True:
            block = src.read(chunk)
            if not block:
                break
            dst.write(block)
            self.total += len(block)
            now = time.monotonic()
            if now - self._last_render > 0.2:
                self._last_render = now
                rate = self.total / max(now - self._start, 1e-9) / 1e6
                print(f"\r{self.label}: {_fmt_mb(self.total)} ({rate:.1f} MB/s)   ", end="", flush=True)
        print(f"\r{self.label}: {_fmt_mb(self.total)} done" + " " * 20, flush=True)


def parse_target(spec):
    """'ssh://user@host:2222' / 'user@host' -> (target, port|None)."""
    spec = spec.strip()
    if spec.startswith("ssh://"):
        spec = spec[len("ssh://") :]
    port = None
    hostpart = spec.rsplit("@", 1)[-1]
    if ":" in hostpart:
        spec, _, port_s = spec.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            raise PullError(f"Bad port in '{spec}:{port_s}'")
    if not spec or "@" not in spec:
        raise PullError(f"SSH target must look like user@host (got '{spec}')")
    return spec, port


def _ssh_cmd(target, port, remote_command, key=None):
    args = [
        "ssh",
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if key:
        args += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    if port:
        args += ["-p", str(port)]
    return [*args, target, remote_command]


def _scp_cmd(target, port, remote_path, local_path, key=None, legacy=False):
    args = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if legacy:
        # OpenSSH >= 9 speaks SFTP by default; odoo.sh offers no sftp subsystem
        args += ["-O"]
    if key:
        args += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    if port:
        args += ["-P", str(port)]
    return [*args, f"{target}:{remote_path}", str(local_path)]


def probe(target, port=None, key=None):
    """Fail fast with the real SSH error if we cannot connect/authenticate."""
    proc = subprocess.run(_ssh_cmd(target, port, "echo ODOOCTL_OK", key=key), capture_output=True)
    if proc.returncode != 0 or b"ODOOCTL_OK" not in (proc.stdout or b""):
        err = ((proc.stderr or b"") + (proc.stdout or b"")).decode(errors="replace").strip()
        raise PullError(
            f"Cannot reach {target} over SSH.\n{err}\n\n"
            "Hints:\n"
            "- Use the exact string from your Odoo.sh 'SSH' button "
            "(it can look like 'ssh 1234567@acme.odoo.com').\n"
            "- Add your public key under odoo.sh project Settings -> Keys.\n"
            f"- Test manually: ssh {target} 'ls -1 /backup.daily/'"
        )


def find_remote_backup(target, port=None, path=None, key=None):
    """Newest *.sql.gz in the given path, else in Odoo.sh's backup dirs.

    Returns {"sql_gz": remote path, "mirror": remote dir | None}. Odoo.sh keeps
    the filestore in a sibling directory named like the dump without extension,
    mirroring $HOME (filestore under home/odoo/data).
    """
    probe(target, port, key=key)
    dirs = [path] if path else ODOO_SH_BACKUP_DIRS
    looked = []
    for d in dirs:
        looked.append(d)
        proc = subprocess.run(
            _ssh_cmd(target, port, f"ls -1t {d}/*.sql.gz 2>/dev/null | head -n 1", key=key),
            capture_output=True,
        )
        first = proc.stdout.decode(errors="replace").strip().splitlines()
        if proc.returncode == 0 and first and first[0].strip():
            sql_gz = first[0].strip()
            mirror = sql_gz[: -len(".sql.gz")] if sql_gz.endswith(".sql.gz") else None
            if mirror:
                chk = subprocess.run(
                    _ssh_cmd(target, port, f"[ -d '{mirror}' ] && echo yes || echo no", key=key),
                    capture_output=True,
                )
                if "yes" not in chk.stdout.decode(errors="replace"):
                    mirror = None
            return {"sql_gz": sql_gz, "mirror": mirror}
    raise PullError(
        "No backup (*.sql.gz) found on remote. Looked in: "
        + ", ".join(looked)
        + "\nPass --path /path/to/backup.sql.gz explicitly."
    )


def _stream_remote_tar(target, port, remote_dir, remote_sub, dest: Path, key=None, progress=True):
    """ssh 'tar -C <dir> -czf - <sub>' piped into a local tar extraction."""
    src = subprocess.Popen(
        _ssh_cmd(target, port, f"tar -C {remote_dir} -czf - {remote_sub}", key=key),
        stdout=subprocess.PIPE,
    )
    dst = subprocess.Popen(
        ["tar", "-xzf", "-", "-C", str(dest)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pipe = _ProgressPipe(label="filestore") if progress else None
    try:
        if pipe:
            pipe.pump(src.stdout, dst.stdin)
        else:
            while True:
                block = src.stdout.read(1 << 20)
                if not block:
                    break
                dst.stdin.write(block)
    finally:
        try:
            dst.stdin.close()
        except BrokenPipeError:
            pass
        src.stdout.close()
    dst_rc = dst.wait()
    src_rc = src.wait()
    if dst_rc != 0 or src_rc != 0:
        raise PullError(f"Streaming '{remote_sub}' from remote failed (ssh rc={src_rc}, tar rc={dst_rc}).")


def remote_dir_size(target, port, path, key=None):
    """`du -sm` on the remote path -> e.g. '2.3 GB', or None."""
    proc = subprocess.run(
        _ssh_cmd(target, port, f"du -sm {path} 2>/dev/null | cut -f1", key=key),
        capture_output=True,
    )
    try:
        mb = int(proc.stdout.decode().strip())
    except ValueError:
        return None
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb} MB"


def download(target, port, remote, dest_dir, key=None, with_filestore=False):
    """Download an odooctl/odoo.sh raw backup into dest_dir/<base>/ as a bundle.

    By default only the .sql.gz is fetched (dev copies rarely need attachments);
    pass with_filestore=True to also stream the remote home/odoo/data tree.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(remote["sql_gz"]).name
    base = name[: -len(".sql.gz")] if name.endswith(".sql.gz") else Path(name).stem
    bundle = dest_dir / base
    bundle.mkdir(parents=True, exist_ok=True)

    local_sql = bundle / name
    if local_sql.exists() and local_sql.stat().st_size > 0:
        print(f"reusing cached {name} ({_fmt_mb(local_sql.stat().st_size)})")
    else:
        proc = subprocess.run(
            _scp_cmd(target, port, remote["sql_gz"], local_sql, key=key), capture_output=True
        )
        if proc.returncode != 0 and b"subsystem request failed" in (proc.stderr or b""):
            proc = subprocess.run(
                _scp_cmd(target, port, remote["sql_gz"], local_sql, key=key, legacy=True), capture_output=True
            )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip()
            raise PullError(f"Download failed: {err}")

    mirror = remote.get("mirror")
    if mirror and with_filestore:
        data_dir = f"{mirror}/home/odoo/data"
        chk = subprocess.run(
            _ssh_cmd(target, port, f"[ -d '{data_dir}' ] && echo yes || echo no", key=key),
            capture_output=True,
        )
        if "yes" in chk.stdout.decode(errors="replace"):
            size = remote_dir_size(target, port, data_dir, key=key)
            if size:
                print(f"filestore on remote: {size} (compressed stream)")
            _stream_remote_tar(target, port, mirror, "home/odoo/data", bundle, key=key)
    if not with_filestore:
        # drop leftovers of an aborted --with-filestore run so a half filestore
        # never sneaks into the restore
        shutil.rmtree(bundle / "home", ignore_errors=True)
    return bundle
