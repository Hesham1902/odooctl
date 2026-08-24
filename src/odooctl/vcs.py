import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(project_path, *args):
    proc = subprocess.run(
        ["git", "-C", str(project_path), *args],
        capture_output=True,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="replace").strip()
        raise GitError(err or f"git {args[0]} failed in {project_path}")
    return proc.stdout.decode(errors="replace")


def changed_files(project_path, ref=None):
    """Files changed vs `ref` (default: uncommitted changes vs HEAD)."""
    spec = [ref] if ref else ["HEAD"]
    out = _git(project_path, "diff", "--name-only", *spec)
    # include staged+unstaged when comparing against HEAD explicitly is enough;
    # also pick up untracked files under the repo
    if not ref:
        try:
            others = _git(project_path, "ls-files", "--others", "--exclude-standard")
            out += others
        except GitError:
            pass
    return [line.strip() for line in out.splitlines() if line.strip()]


def modules_from_files(files, custom_addons_dir):
    """Map changed file paths to custom addon names."""
    root = Path(custom_addons_dir).resolve()
    mods = set()
    for f in files:
        try:
            p = Path(f)
            if not p.is_absolute():
                p = root.parent / f  # project-relative path
            p = p.resolve()
            p.relative_to(root)
        except (ValueError, OSError):
            continue
        rel = p.relative_to(root)
        if len(rel.parts) >= 1 and rel.parts[0]:
            mods.add(rel.parts[0])
    return sorted(mods)


def changed_modules(project_path, custom_addons_dir, ref=None):
    files = changed_files(project_path, ref)
    return modules_from_files(files, custom_addons_dir)
