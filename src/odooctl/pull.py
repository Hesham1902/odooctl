import gzip
import shlex
import shutil
import subprocess
import tarfile
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

# odoo.sh's SSH gateway intermittently rejects exec requests
# ("exec request failed on channel 0") on connections that are otherwise fine;
# a fresh attempt right after usually succeeds.
_TRANSIENT_EXEC_ERROR = "exec request failed"
_SSH_ATTEMPTS = 5
_SSH_RETRY_DELAY = 1.5

# characters that make a remote shell word unsafe to leave unquoted
_UNSAFE_WORD_CHARS = set(" \t\n'\"\\$`&|;<>(){}*?[]#!")


def _shell_word(path):
    """Emit a path as one remote shell word: bare when safe (so ~ and globs
    expand on the remote side), quoted otherwise."""
    if path and not any(ch in path for ch in _UNSAFE_WORD_CHARS):
        return path
    return shlex.quote(path)


class PullError(RuntimeError):
    pass


def _fmt_mb(n):
    return f"{n / 1e6:.1f} MB"


class _ProgressReader:
    """Read a stream while printing a running byte total on one line."""

    def __init__(self, src, label="downloading"):
        self.src = src
        self.label = label
        self.total = 0
        self._start = time.monotonic()
        self._last_render = 0.0

    def read(self, size=-1):
        block = self.src.read(size)
        if not block:
            return block
        self.total += len(block)
        now = time.monotonic()
        if now - self._last_render > 0.2:
            self._last_render = now
            rate = self.total / max(now - self._start, 1e-9) / 1e6
            print(f"\r{self.label}: {_fmt_mb(self.total)} ({rate:.1f} MB/s)   ", end="", flush=True)
        return block

    def finish(self):
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


def _retry_transient(run_fn, executable):
    """Re-run run_fn while the remote transiently rejects the exec request."""
    try:
        proc = run_fn()
    except FileNotFoundError as exc:
        raise PullError(f"{executable} was not found on PATH. Install it and try again.") from exc
    for _ in range(_SSH_ATTEMPTS - 1):
        if proc.returncode == 0:
            break
        if _TRANSIENT_EXEC_ERROR not in (proc.stderr or b"").decode(errors="replace"):
            break
        time.sleep(_SSH_RETRY_DELAY)
        try:
            proc = run_fn()
        except FileNotFoundError as exc:
            raise PullError(f"{executable} was not found on PATH. Install it and try again.") from exc
    return proc


def _ssh_exec(target, port, command, key=None):
    """One remote command over SSH, retrying odoo.sh's transient exec rejections."""
    return _retry_transient(
        lambda: subprocess.run(_ssh_cmd(target, port, command, key=key), capture_output=True),
        "ssh",
    )


def _unreachable(target, err):
    return PullError(
        f"Cannot reach {target} over SSH.\n{err}\n\n"
        "Hints:\n"
        "- Use the exact string from your Odoo.sh 'SSH' button "
        "(it can look like 'ssh 1234567@acme.odoo.com').\n"
        "- Add your public key under odoo.sh project Settings -> Keys.\n"
        f"- Test manually: ssh {target} 'ls -1 /backup.daily/'"
    )


def probe(target, port=None, key=None):
    """Fail fast with the real SSH error if we cannot connect/authenticate."""
    proc = _ssh_exec(target, port, "echo ODOOCTL_OK", key=key)
    if proc.returncode != 0 or b"ODOOCTL_OK" not in (proc.stdout or b""):
        err = ((proc.stderr or b"") + (proc.stdout or b"")).decode(errors="replace").strip()
        raise _unreachable(target, err)


def _scan_script(dirs):
    """One remote script: newest *.sql.gz across dirs, plus the mirror check.

    Everything must run as a single SSH exec: the odoo.sh gateway rejects
    rapid successive connections, so discovery must not open one channel
    per directory. Dir words stay unquoted when safe so the remote shell
    expands a leading ~ itself (odoo.sh's shell also expands ~ inside
    parameter-expansion patterns, which breaks ${d#~} normalization).
    """
    words = " ".join(_shell_word(d) for d in dirs)
    return (
        "echo ODOOCTL_OK; "
        f"for d in {words}; do "
        'f="$(ls -1t "$d"/*.sql.gz 2>/dev/null | head -n 1)"; '
        '[ -n "$f" ] || continue; '
        'echo "SQL_GZ=$f"; '
        'm="${f%.sql.gz}"; '
        '[ "$m" != "$f" ] && [ -d "$m" ] && echo "MIRROR=$m"; '
        "break; "
        "done"
    )


def _file_script(path):
    """One remote script: use an explicit dump file (path ends in .sql.gz)."""
    return (
        "echo ODOOCTL_OK; "
        f"f={_shell_word(path)}; "
        '[ -f "$f" ] || exit 0; '
        'echo "SQL_GZ=$f"; '
        'm="${f%.sql.gz}"; '
        '[ "$m" != "$f" ] && [ -d "$m" ] && echo "MIRROR=$m"'
    )


def find_remote_backup(target, port=None, path=None, key=None):
    """Newest *.sql.gz in the given path, else in Odoo.sh's backup dirs.

    Returns {"sql_gz": remote path, "mirror": remote dir | None}. Odoo.sh keeps
    the filestore in a sibling directory named like the dump without extension,
    mirroring $HOME (filestore under home/odoo/data). `path` may be a directory
    to scan or a direct path to a .sql.gz file.
    """
    if path and path.endswith(".sql.gz"):
        script, looked = _file_script(path), [path]
    else:
        dirs = [path] if path else ODOO_SH_BACKUP_DIRS
        script, looked = _scan_script(dirs), dirs
    proc = _ssh_exec(target, port, script, key=key)
    out = proc.stdout.decode(errors="replace")
    if proc.returncode != 0 or "ODOOCTL_OK" not in out:
        err = ((proc.stderr or b"") + (proc.stdout or b"")).decode(errors="replace").strip()
        raise _unreachable(target, err)
    sql_gz = mirror = None
    for line in out.splitlines():
        if sql_gz is None and line.startswith("SQL_GZ="):
            sql_gz = line[len("SQL_GZ=") :].strip()
        elif sql_gz is not None and mirror is None and line.startswith("MIRROR="):
            mirror = line[len("MIRROR=") :].strip()
    if not sql_gz:
        raise PullError(
            "No backup (*.sql.gz) found on remote. Looked in: "
            + ", ".join(looked)
            + "\nPass --path /path/to/backup.sql.gz explicitly."
        )
    return {"sql_gz": sql_gz, "mirror": mirror}


def _stream_remote_tar(target, port, remote_dir, remote_sub, dest: Path, key=None, progress=True):
    """Extract a remote tar stream with Python so Windows needs no local tar."""
    src = None
    try:
        src = subprocess.Popen(
            _ssh_cmd(target, port, f"tar -C {remote_dir} -czf - {remote_sub}", key=key),
            stdout=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PullError(f"{exc.filename} was not found on PATH. Install it and try again.") from exc
    reader = _ProgressReader(src.stdout, label="filestore") if progress else src.stdout
    root = dest.resolve()
    failure = None
    try:
        with tarfile.open(fileobj=reader, mode="r|gz") as archive:
            for member in archive:
                target_path = (dest / member.name).resolve()
                if not target_path.is_relative_to(root):
                    raise PullError(f"Remote archive contains an unsafe path: {member.name}")
                if member.issym() or member.islnk():
                    link_path = (target_path.parent / member.linkname).resolve()
                    if not link_path.is_relative_to(root):
                        raise PullError(f"Remote archive contains an unsafe link: {member.name}")
                archive.extract(member, path=dest)
    except (OSError, tarfile.TarError, PullError) as exc:
        failure = exc
    finally:
        src.stdout.close()
    src_rc = src.wait()
    if progress:
        reader.finish()
    if failure:
        raise PullError(f"Streaming '{remote_sub}' from remote failed: {failure}") from failure
    if src_rc != 0:
        raise PullError(f"Streaming '{remote_sub}' from remote failed (ssh rc={src_rc}).")


def remote_dir_size(target, port, path, key=None):
    """`du -sm` on the remote path -> e.g. '2.3 GB', or None."""
    proc = _ssh_exec(
        target, port, f"du -sm {path} 2>/dev/null | cut -f1", key=key
    )
    try:
        mb = int(proc.stdout.decode().strip())
    except ValueError:
        return None
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb} MB"


def _is_complete_gzip(path: Path) -> bool:
    """A partial/aborted scp leaves a truncated file that gzip can't fully decode.

    Reusing it silently feeds garbage into psql later, so verify the stream
    actually decompresses to its end-of-stream marker before trusting the cache.
    Full decompression is the expensive fallback for when we can't just compare
    sizes with the remote (see _remote_file_size) - it pays for itself only once,
    right after a fresh download, or when the remote size can't be read.
    """
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


def _remote_file_size(target, port, path, key=None):
    """Size in bytes of `path` on the remote, or None if it can't be read."""
    proc = _ssh_exec(
        target,
        port,
        f"stat -c%s '{path}' 2>/dev/null || stat -f%z '{path}' 2>/dev/null",
        key=key,
    )
    out = proc.stdout.decode(errors="replace").strip()
    return int(out) if out.isdigit() else None


def _cached_sql_gz_is_reusable(target, port, remote, local_sql, key=None):
    """Cheap first: does the cache match the remote's size? Only pay for a full
    gzip decompress when the remote size can't be determined over SSH."""
    if not (local_sql.exists() and local_sql.stat().st_size > 0):
        return False
    remote_size = _remote_file_size(target, port, remote["sql_gz"], key=key)
    if remote_size is not None:
        return remote_size == local_sql.stat().st_size
    return _is_complete_gzip(local_sql)


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
    if _cached_sql_gz_is_reusable(target, port, remote, local_sql, key=key):
        print(f"reusing cached {name} ({_fmt_mb(local_sql.stat().st_size)})")
    else:
        if local_sql.exists():
            print(f"cached {name} is incomplete or corrupt, re-downloading")
            local_sql.unlink()

        def _attempt(legacy=False):
            return subprocess.run(
                _scp_cmd(target, port, remote["sql_gz"], local_sql, key=key, legacy=legacy),
                capture_output=True,
            )

        proc = _retry_transient(_attempt, "scp")
        if proc.returncode != 0 and b"subsystem request failed" in (proc.stderr or b""):
            proc = _retry_transient(lambda: _attempt(legacy=True), "scp")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip()
            raise PullError(f"Download failed: {err}")

    mirror = remote.get("mirror")
    if mirror and with_filestore:
        data_dir = f"{mirror}/home/odoo/data"
        chk = _ssh_exec(target, port, f"[ -d '{data_dir}' ] && echo yes || echo no", key=key)
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
