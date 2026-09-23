"""Find published odooctl releases without making network problems fatal."""

import json
import math
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path

from . import __version__
from .registry import _config_dir

REPO_URL = "https://github.com/Hesham1902/odooctl"
RELEASE_API_URL = "https://api.github.com/repos/Hesham1902/odooctl/releases/latest"
CHECK_INTERVAL = 24 * 3600
RETRY_INTERVAL = 60 * 60
REQUEST_TIMEOUT = 2
_CACHE_SOURCE = "github-release"
_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
_TAG = re.compile(r"^v(\d+\.\d+\.\d+)$")


def _cache_path():
    return _config_dir() / "update-check.json"


def _version_tuple(value):
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        return None
    parts = tuple(int(piece) for piece in value.split("."))
    return parts + (0,) * (3 - len(parts))


def _fetch_latest_version():
    request = urllib.request.Request(
        RELEASE_API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"odooctl/{__version__}"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        release = json.loads(response.read(1024 * 1024))
    if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
        raise ValueError("No published stable release")
    tag = release.get("tag_name")
    match = _TAG.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError("Invalid release tag")
    return match.group(1)


def _load_cache():
    try:
        data = json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("source") != _CACHE_SOURCE:
        return {}
    checked_at = data.get("checked_at")
    latest = data.get("latest")
    if (
        isinstance(checked_at, bool)
        or not isinstance(checked_at, (int, float))
        or (isinstance(checked_at, float) and not math.isfinite(checked_at))
        or checked_at < 0
        or checked_at > time.time()
        or (latest is not None and _version_tuple(latest) is None)
    ):
        return {}
    return data


def _store_cache(latest, failed=False):
    """Replace the cache atomically so overlapping commands cannot truncate it."""
    temporary = None
    try:
        directory = _config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(
                {
                    "source": _CACHE_SOURCE,
                    "checked_at": time.time(),
                    "latest": latest,
                    "failed": failed,
                },
                stream,
            )
        os.replace(temporary, _cache_path())
    except OSError:
        pass  # A read-only config directory must not break a command.
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def check_for_update():
    """Return a newer published version, or None when unavailable or current."""
    if os.environ.get("ODOOCTL_NO_UPDATE_CHECK", "").lower() in {"1", "true", "yes", "on"}:
        return None
    cache = _load_cache()
    interval = RETRY_INTERVAL if cache.get("failed") else CHECK_INTERVAL
    if time.time() - cache.get("checked_at", 0) < interval:
        latest = cache.get("latest")
    else:
        try:
            latest = _fetch_latest_version()
            if _version_tuple(latest) is None:
                raise ValueError("Invalid release version")
        except Exception:
            latest = cache.get("latest")
            _store_cache(latest, failed=True)
        else:
            _store_cache(latest)
    current_tuple = _version_tuple(__version__)
    latest_tuple = _version_tuple(latest)
    if current_tuple is None or latest_tuple is None or latest_tuple <= current_tuple:
        return None
    return latest


def release_url(latest):
    """Build a trusted release URL from a validated version."""
    if _version_tuple(latest) is None:
        raise ValueError("Invalid release version")
    return f"{REPO_URL}/releases/tag/v{latest}"


def _clone_dir():
    """The git clone this tool runs from, when installed editable."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            return parent
    return None


def update_notice(latest):
    lines = [f"odooctl {latest} is available (you have {__version__})."]
    lines.append(f"Release and downloads: {release_url(latest)}")
    lines.append(f"CLI update steps: {REPO_URL}#update-notices")
    clone = _clone_dir()
    if clone:
        lines.append(f"Source checkout: {clone}. Pull changes there, then reinstall using your original method.")
    return "\n".join(lines)
