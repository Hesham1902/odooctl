"""Public CLI entry point.

Command implementations live in :mod:`odooctl.commands` by user-facing
family. The domain modules remain imported here as a compatibility surface
for integrations and tests that historically accessed them through
``odooctl.cli``.
"""

import subprocess

from . import admin, compose, dbdiff, logparse, provision, registry, space, testing, vcs, watcher
from . import deps as deps_mod
from . import icons as icons_mod
from . import pull as pull_mod
from . import restore as restore_mod
from . import sanitize as sanitize_mod

# Importing each family registers its commands on ``root.main``.
from .commands import common, database, development, projects, root, runtime, storage  # noqa: F401
from .manifest import list_addons

CONTEXT_SETTINGS = root.CONTEXT_SETTINGS
ODOO_CONF = common.ODOO_CONF
main = root.main
GC_KEEP_BACKUPS = storage.GC_KEEP_BACKUPS
GC_KEEP_LOGS = storage.GC_KEEP_LOGS


def _entry(name):
    return common.entry(name)


def _need_docker():
    return common.need_docker()


def _pick_db(slug, project_entry, db):
    return common.pick_db(project_entry, db)


def _print_project_line(slug, project_entry):
    return common.print_project_line(slug, project_entry)


def _resolve_changed(project_entry, since):
    return development.resolve_changed(project_entry, since)


def _wait_http(port, timeout=120):
    return common.wait_http(port, timeout=timeout)


__all__ = [
    "CONTEXT_SETTINGS",
    "GC_KEEP_BACKUPS",
    "GC_KEEP_LOGS",
    "ODOO_CONF",
    "_entry",
    "_need_docker",
    "_pick_db",
    "_print_project_line",
    "_resolve_changed",
    "_wait_http",
    "admin",
    "compose",
    "dbdiff",
    "deps_mod",
    "icons_mod",
    "list_addons",
    "logparse",
    "main",
    "provision",
    "pull_mod",
    "registry",
    "restore_mod",
    "sanitize_mod",
    "space",
    "subprocess",
    "testing",
    "vcs",
    "watcher",
]


if __name__ == "__main__":
    main()
