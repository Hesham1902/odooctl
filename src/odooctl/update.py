"""Update notice: compare the running version against the repo's latest.

The version of record is `__version__` in src/odooctl/__init__.py on GitHub
main. The check fetches that file raw at most once a day, caches the result
under the config dir, and never lets a network problem slow the CLI down or
break it. Set ODOOCTL_NO_UPDATE_CHECK=1 to opt out.
"""

import json
import os
import re
import time
import urllib.request
from pathlib import Path

from . import __version__
from .registry import _config_dir

REPO_URL = "https://github.com/Hesham1902/odooctl"
REMOTE_VERSION_URL = f"{REPO_URL}/raw/main/src/odooctl/__init__.py"
CHECK_INTERVAL = 24 * 3600
REQUEST_TIMEOUT = 3

_VERSION_LINE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]")


def _cache_path():
    return _config_dir() / "update-check.json"


def _version_tuple(value):
    parts = []
    for piece in value.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _parse_remote_version(text):
    for line in text.splitlines():
        match = _VERSION_LINE.match(line.strip())
        if match:
            return match.group(1)
    return None


def _fetch_latest_version():
    with urllib.request.urlopen(REMOTE_VERSION_URL, timeout=REQUEST_TIMEOUT) as response:
        text = response.read(65536).decode("utf-8", errors="replace")
    return _parse_remote_version(text)


def _load_cache():
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(latest):
    try:
        _config_dir().mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps({"checked_at": time.time(), "latest": latest}))
    except OSError:
        pass  # a read-only home must never break the CLI


def check_for_update():
    """Return the newer remote version string, or None when up to date.

    Never raises: offline, blocked, or slow network simply means no notice,
    and the next run retries (a failed fetch is not cached).
    """
    if os.environ.get("ODOOCTL_NO_UPDATE_CHECK"):
        return None
    cache = _load_cache()
    if time.time() - cache.get("checked_at", 0) < CHECK_INTERVAL:
        latest = cache.get("latest")
    else:
        try:
            latest = _fetch_latest_version()
        except OSError:
            return None
        _store_cache(latest)
    if not latest or _version_tuple(latest) <= _version_tuple(__version__):
        return None
    return latest


def _clone_dir():
    """The git clone this tool runs from, when installed editable."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None


def update_notice(latest):
    lines = [f"odooctl {latest} is available (you have {__version__})."]
    clone = _clone_dir()
    if clone:
        lines.append(f"Update: cd {clone} && git pull, then reinstall if you used pipx/uv.")
    else:
        lines.append(f"Update: git pull your odooctl clone and reinstall - {REPO_URL}#installation")
    return "\n".join(lines)
