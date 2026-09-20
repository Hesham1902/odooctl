"""Environment adjustments needed when the GUI starts outside a terminal."""

import os
import sys
from pathlib import Path


def prepare_gui_environment():
    """Add common Docker and OpenSSH locations to PATH for desktop launches."""
    if sys.platform == "darwin":
        candidates = (
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
            Path("/Applications/Docker.app/Contents/Resources/bin"),
        )
    elif os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates = (
            program_files / "Docker" / "Docker" / "resources" / "bin",
            program_files / "OpenSSH",
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "OpenSSH",
        )
    else:
        candidates = ()

    existing = os.environ.get("PATH", "").split(os.pathsep)
    additions = [str(path) for path in candidates if path.is_dir() and str(path) not in existing]
    if additions:
        os.environ["PATH"] = os.pathsep.join([*additions, *existing])
