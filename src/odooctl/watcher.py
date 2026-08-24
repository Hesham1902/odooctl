import time
from pathlib import Path


def snapshot(root, exts=("py",)):
    """{path: mtime} for every matching file under root (poll-based, bind-mount safe)."""
    sig = {}
    root = Path(root)
    for p in root.rglob("*"):
        if p.suffix.lstrip(".").lower() in exts and p.is_file():
            try:
                sig[str(p)] = p.stat().st_mtime
            except OSError:
                continue
    return sig


def diff_files(old, new):
    changed = [p for p in new.keys() | old.keys() if old.get(p) != new.get(p)]
    return sorted(changed)


def watch(root, exts=("py",), interval=0.5, debounce=0.8,
          echo=lambda msg: print(msg), restart=None, max_cycles=None):
    """Poll `root`; call restart() after each settled change. Blocks until Ctrl-C."""
    echo(f"watching {root} (*.{', *.'.join(sorted(exts))})")
    last = snapshot(root, exts)
    cycles = 0
    while True:
        time.sleep(interval)
        current = snapshot(root, exts)
        if current == last:
            continue
        changed = diff_files(last, current)
        # debounce: editors write bursts; settle before restarting
        time.sleep(debounce)
        current = snapshot(root, exts)
        changed = diff_files(last, current)
        last = current
        names = ", ".join(Path(c).name for c in changed[:4])
        extra = f" (+{len(changed) - 4} more)" if len(changed) > 4 else ""
        echo(f"change detected ({len(changed)} file(s)): {names}{extra}")
        if restart:
            restart()
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            return
