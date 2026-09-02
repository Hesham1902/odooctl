# odooctl

One CLI for all your local Odoo docker environments.

If you maintain more than one Odoo project locally, you juggle different container
names, ports, databases and long `docker compose exec ...` incantations for each one.
`odooctl` discovers your projects once, stores them in a registry, and gives you one
uniform command surface - from starting/stopping environments and auto-restarting on
code changes, to pulling the latest backup over SSH, restoring Odoo.sh backups safely
(crons paused, mail purged), bootstrapping brand-new projects in seconds, and running
addon tests in throwaway databases.

Works on **macOS** and **Linux**.

---

## Contents

**Getting started**

1. [Requirements](#requirements)
2. [Installation](#installation) - [macOS](#macos) · [Linux](#linux)
3. [First run: discovering projects](#first-run-discovering-your-projects)
4. [Core concepts](#core-concepts)

**Feature guides**

5. [`status` - see everything at a glance](#status---see-everything-at-a-glance)
6. [`up` / `down` / `restart` - lifecycle management](#up--down--restart---lifecycle-management)
7. [`logs` and `open` - following output, opening the app](#logs-and-open---following-output-opening-the-app)
8. [`dev` - auto-restart on code changes](#dev---auto-restart-on-code-changes)
9. [`diff` - compare module state between databases](#diff---compare-module-state-between-databases)
10. [`deps` - dependency tree of an addon](#deps---dependency-tree-of-an-addon)
11. [`psql` - direct database access](#psql---direct-database-access)
12. [`shell` - Odoo ORM REPL](#shell---odoo-orm-repl)
13. [`addons` - inspecting custom addons](#addons---inspecting-custom-addons)
14. [`upgrade` - upgrading modules safely](#upgrade---upgrading-modules-safely)
15. [`backup` - snapshotting a database](#backup---snapshotting-a-database)
16. [`restore` - restoring backups (incl. Odoo.sh zips)](#restore---restoring-backups-incl-odoosh-zips)
17. [`sanitize` - make a restored prod DB safe](#sanitize---make-a-restored-prod-db-safe)
18. [`fix-icons` - bring missing menu icons back](#fix-icons---bring-missing-menu-icons-back)
19. [`pull` - latest backup over SSH, restored, in one command](#pull---latest-backup-over-ssh-restored-in-one-command)
20. [`reset-admin` - regaining admin access](#reset-admin---regaining-admin-access)
21. [`reset` - wiping a database](#reset---wiping-a-database)
22. [`init` - bootstrapping a new project](#init---bootstrapping-a-new-project)
23. [`test` - running addon tests in isolation](#test---running-addon-tests-in-isolation)
24. [`space` - where your disk space went](#space---where-your-disk-space-went)
25. [`gc` - reclaiming wasted space](#gc---reclaiming-wasted-space)
26. [`remove` - deleting a project](#remove---deleting-a-project)

**Reference**

27. [Configuration file reference](#configuration-file-reference)
28. [macOS vs Linux notes](#macos-vs-linux-notes)
29. [Troubleshooting](#troubleshooting)
30. [Development](#development)

---

## Requirements

- **Docker with Compose v2** - check with `docker compose version`.
  (The old `docker-compose` binary is *not* used.)
- **Python 3.10 or newer** - `python3 --version`
- Your Odoo projects are laid out the standard way: one folder per project containing
  `docker-compose.yml` (a `web` service and a `db` service), `custom_addons/`,
  and `config/odoo.conf`.

> **Note:** Windows is not supported directly. If you develop inside **WSL2**, follow
> the Linux instructions.

## Installation

Both platforms install the same way - pick one of the three methods below. The only
real difference between macOS and Linux is getting Docker itself running (see the
per-platform sections).

### Method 1: pipx (recommended)

```bash
pipx install <path-to-this-repo>
```

`pipx` installs the tool into an isolated virtualenv and puts `odooctl` on your PATH
(usually `~/.local/bin`). Verify:

```bash
odooctl --version
```

### Method 2: uv

```bash
uv tool install <path-to-this-repo>
```

### Method 3: plain venv

```bash
cd <path-to-this-repo>
python3 -m venv .venv
.venv/bin/pip install -e .
export PATH="$PWD/.venv/bin:$PATH"     # add this line to your ~/.zshrc or ~/.bashrc
```

### macOS

1. Install **Docker Desktop** (https://www.docker.com/products/docker-desktop/) if you
   haven't already, launch it once, and wait until the whale icon shows
   "running".
2. Check: `docker compose version`
3. Install the CLI with any method above.

### Linux

1. Install Docker Engine + the compose plugin using your distribution's instructions
   (e.g. https://docs.docker.com/engine/install/ubuntu/). Or use Docker Desktop for
   Linux.
2. Make sure your user can talk to the daemon without sudo (otherwise you must prefix
   every `odooctl` call with `sudo`, which is not recommended):
   ```bash
   sudo usermod -aG docker $USER    # then log out and back in
   ```
3. Start the daemon and enable it at boot:
   ```bash
   sudo systemctl start docker && sudo systemctl enable docker
   ```
4. Check: `docker compose version`
5. Install the CLI with any method above.

## First run: discovering your projects

```bash
odooctl discover                        # scan saved roots + your current directory
odooctl discover --root ~/code          # also scan (and remember) another folder
odooctl discover --forget-root ~/old    # stop scanning a saved root
odooctl discover -v                     # show every root and every compose file skipped
```

`discover` walks your work folders, up to three folders deep, looking for compose files
(`compose.yaml`, `compose.yml`, `docker-compose.yaml` or `docker-compose.yml`) whose
services look like an Odoo setup: one service whose name, image, build or volumes mention
`odoo`, plus one Postgres/PostGIS service. Every hit is registered under a slug derived
from its web container's name, e.g. container `acme_web` becomes project **`acme`**.

Your current directory is always scanned as well, so running `odooctl discover` from
inside a project finds it. Only roots given with `--root` are remembered. The result
looks like:

```
Found 3 project(s):
acme           /home/you/work/acme              http:8070   pg:5470
beta           /home/you/work/beta              http:8071   pg:5455
gamma          /home/you/work/gamma             http:8056   pg:5456
```

Nothing is started or modified by discovery - it only reads files.

When nothing is found, `discover` prints what it looked at instead of a bare error:
every root (`missing` or `scanned, N compose files`), every compose file it saw but
rejected with the reason (too deep, no Odoo web service, no Postgres service, YAML
error, duplicate slug), and a hint matching the situation.

```bash
odooctl projects                  # list registered projects later
```

**Prefix matching:** every command that takes a PROJECT argument accepts a unique
prefix instead of the full name. `odooctl logs beta` targets `beta`; `odooctl logs b`
also works while it is unambiguous. If a prefix matches multiple projects you get an
error listing them.

## Core concepts

- **Registry**: `~/.config/odooctl/config.json` maps slugs to project paths, service
  names, ports and detected Odoo version. See
  [Configuration file reference](#configuration-file-reference).
- **Scan roots**: the folders `discover` searches, saved in the registry. Defaults are
  `~/Developer/Work` and `~/odoo-projects`; add more with `--root`, drop them with
  `--forget-root`. Your current directory is scanned every time but never saved.
- **Image naming**: Docker Compose names built images `<folder>-<service>`
  (folder `acme` → image `acme-web`). `odooctl init` exploits this to reuse existing
  images instantly; see the [`init`](#init---bootstrapping-a-new-project) section.
- **Safety defaults**: destructive operations (`restore`, `reset`) ask for
  confirmation before dropping data; `test` always runs against a throwaway database,
  never yours.

---

# Command reference

The command surface stays deliberately flat for fast daily use (`odooctl up acme`,
not `odooctl runtime up acme`). `odooctl --help` groups commands by purpose so the
less frequent operations remain easy to discover. Compatibility aliases are kept
for renamed commands: `url` still invokes `open`, and `df` still invokes `space`.

| Family | Command | Purpose |
|---|---|---|
| Project management | `discover` | (Re)scan folders for Odoo projects |
| Project management | `projects` | List registered projects |
| Project management | `init` | Bootstrap a new project |
| Project management | `remove` | Stop, unregister and optionally delete a project |
| Runtime | `status` | Containers + databases, all projects or one |
| Runtime | `up` / `down` | Start or stop a project's containers |
| Runtime | `restart` | Restart one service |
| Runtime | `logs` | Show / stream logs (`--errors`: only errors + tracebacks) |
| Runtime | `dev` | Watch `custom_addons` and restart web on source changes |
| Runtime | `open` (`url`) | Open the project in a browser |
| Development | `psql` | Interactive SQL session |
| Development | `shell` | Interactive `odoo shell` session |
| Development | `addons` | List custom addons (+ install state) |
| Development | `deps` | Dependency tree, reverse deps, cycle detection |
| Development | `diff` | Compare module state/version between two databases |
| Development | `upgrade` | Upgrade addon(s) - explicit list or git-changed |
| Development | `test` | Run addon tests in disposable databases |
| Database | `backup` / `restore` | Save or restore a database and filestore |
| Database | `pull` | Fetch the latest backup over SSH and restore it |
| Database | `sanitize` | Pause crons, disable mail and scrub PII |
| Database | `reset` / `reset-admin` | Recreate a database or reset its main user |
| Database | `fix-icons` | Re-import menu icons after a restore without filestore |
| Storage | `space` (`df`) | Explain Docker and project disk usage |
| Storage | `gc` | Plan or execute safe cleanup; `--deep` wipes project volumes |

Add global `--debug` before a command to print operation timings:

```bash
odooctl --debug status
```

Detailed documentation for every command follows.

---

## `status` - see everything at a glance

```bash
odooctl status                    # all projects
odooctl status acme               # one project
```

For each project it prints:

- the folder and HTTP/Postgres port mapping,
- each service with running state (green/red),
- all databases present in that project's PostgreSQL instance.

```
acme  (/home/you/work/acme)  http:8070  pg:5470
  db    acme_db: running
  web   acme_web: running
  databases: acme-prod, acme-test
```

Requires a reachable Docker daemon; otherwise you get a clear error reminding you to
start it.

## `up` / `down` / `restart` - lifecycle management

### `up`

```bash
odooctl up acme               # start web+db in background
odooctl up acme --build       # rebuild images first
odooctl up acme --no-wait     # don't poll HTTP readiness
```

After starting, `up` polls `http://localhost:<port>/web/login` until Odoo answers
(up to ~2 min) so you know when you can actually open it. First start of a freshly
built stack takes longer than later ones.

### `down`

```bash
odooctl down acme
```

Stops and removes containers (data volumes in `./data` and `./home` stay on disk).

### `restart`

```bash
odooctl restart acme              # restarts web
odooctl restart acme --service db # restart another service
```

Typical use after editing Python files in `custom_addons`.

## `logs` and `open` - following output, opening the app

```bash
odooctl logs acme             # last 100 lines of the web container
odooctl logs acme -f          # stream live (Ctrl-C to stop)
odooctl logs acme -t 500      # last 500 lines
odooctl logs acme --service db

odooctl logs acme --errors    # only ERROR/CRITICAL + their tracebacks (scans last 2000)
odooctl logs acme -e -f       # stream, filtered to errors
```

```bash
odooctl open acme             # prints http://localhost:<port> and opens browser
# `odooctl url acme` remains a compatibility alias
```

## `dev` - auto-restart on code changes

```bash
odooctl dev acme                          # restart web when .py files change
odooctl dev acme --ext py,xml             # watch more extensions
odooctl dev acme --debounce 1.5           # settle time before restarting
```

Watches `custom_addons/` from the host side and restarts web when Python files
change. This is deliberately a host-side poller: on macOS bind mounts, inotify
events never reach the container, so Odoo's built-in `--dev=reload` usually does not
fire.

What happens when you save a file:

1. The poller (every 0.5 s) notices the change.
2. It waits 0.8 s (`--debounce`) and re-checks, so an editor save burst triggers one
   restart, not five. It prints which files changed.
3. It runs the equivalent of `docker compose restart web`. A few seconds later the
   new code is live; reload your browser tab.

Ctrl-C stops it.

One limit: a restart picks up `.py` changes only. XML and data files need a
module upgrade (`-u`), which a restart does not do. If you edit views often, run
`odooctl upgrade <project> --changed -d <db>` afterwards.

## `diff` - compare module state between databases

```bash
odooctl diff acme acme-prod acme-test
odooctl diff acme acme-prod acme-test -g sale     # filter by name substring
```

Reads `ir_module_module` from both databases and shows every module whose state or
version differs, plus modules that exist in only one of them. Answers "what's
installed differently here?" before you debug environment-specific bugs.

## `deps` - dependency tree of an addon

```bash
odooctl deps acme sale_approval_flow
```

Parses manifests on disk (no database needed) and prints:

- the dependency tree of the module - custom deps with versions, Odoo/core deps marked `[external]`,
- transitive counts (custom vs external),
- who depends on this module (`required by:`),
- a loud warning if the module is part of a dependency cycle.

## `psql` - direct database access

```bash
odooctl psql acme                         # maintenance DB
odooctl psql acme -d acme-prod            # specific application DB
```

Opens an interactive `psql` session inside the db container. Exit with `\q` or
Ctrl-D.

> Why `-d` matters: without a database name psql tries to connect to a database named
> after the *user* (usually `odoo`). That database normally doesn't exist - this is a
> PostgreSQL default, not a bug.

## `shell` - Odoo ORM REPL

```bash
odooctl shell acme                     # single-database project: db picked automatically
odooctl shell acme -d acme-prod        # specific database
```

Drops you into an interactive `odoo shell` (`env['res.partner']` and friends) inside
the web container, with the project's config and the chosen database loaded. The web
container must be running; with exactly one database it is picked automatically,
otherwise pass `-d`.

Typical uses:

- quick data inspection/fixes through the ORM (constraints and business logic apply):
  ```python
  >>> env['sale.order'].search_count([('state', '=', 'draft')])
  >>> u = env['res.users'].browse(2); u.tz = 'Europe/Berlin'; env.cr.commit()
  ```
- testing a snippet before putting it into a module or server action.

Exit with `exit()` or Ctrl-D. Changes are only persisted where you call
`env.cr.commit()` yourself.

## `addons` - inspecting custom addons

```bash
odooctl addons acme                      # table of every custom addon
odooctl addons acme -g payroll           # filter by name substring
odooctl addons acme sale_approval_flow   # full manifest JSON of one module
```

Output joins two sources:

- what's **on disk** in `custom_addons/` (parsed from each `__manifest__.py`),
- what's **in the database** (`ir_module_module`: state + version).

```
MODULE                                   VERSION      STATE        DEPENDS
(db: acme-prod)
account_audit_lock         18.0.1.0.0    installed    account, base
sale_approval_flow         18.0.1.2.0    installed    sale_management, mail
...
```

If the project runs multiple databases you'll be asked which one to read state from:

```
Error: Multiple databases: acme-prod, acme-test
Pick one with --db (-d).
```

With exactly one database it is picked automatically. Pass `--db` explicitly in
scripts.

## `upgrade` - upgrading modules safely

```bash
odooctl upgrade acme sale_approval_flow -d acme-prod          # one module
odooctl upgrade acme mod_a mod_b -d acme-prod                 # several at once (single odoo run)
odooctl upgrade acme --changed -d acme-prod                   # everything changed vs git HEAD
odooctl upgrade acme --changed --since main -d acme-prod      # vs another ref
odooctl upgrade acme my_module -d acme-prod --keep-stopped
```

`--changed` maps `git diff --name-only HEAD` (plus untracked files) onto
`custom_addons/<module>/...` paths, so "upgrade what I'm working on" is one command.

What it does, in order:

1. Checks whether the web service is currently running.
2. If yes, stops it (a running instance would conflict with the upgrade worker).
3. Runs `odoo -u <m1,m2,...> --stop-after-init` in a throwaway container - the modules'
   Python/XML/data is reloaded and validated.
4. Restarts web afterwards (unless `--keep-stopped`).

Streamed output shows the upgrade log live; non-zero exit code means the upgrade
failed.

Options:

| Option | Meaning |
|---|---|
| `--db, -d` (required) | Target database |
| `--keep-stopped` | Leave web stopped afterwards |

## `backup` - snapshotting a database

```bash
odooctl backup acme -d acme-prod
```

Produces, under `<project>/backups/odooctl/<db>_<timestamp>/`:

- `db.dump` - compressed PostgreSQL dump (`pg_dump -Fc`),
- `filestore.tar.gz` - the Odoo filestore for that database (attachments, images),
- `meta.json` - records the source database name and timestamp.

Prints total size and the exact restore command. Run it before risky upgrades,
module installs, or data imports.

Keep only the newest N snapshots of that database (older ones are deleted after
the new backup is written):

```bash
odooctl backup acme -d acme-prod --keep 3
```

## `restore` - restoring backups (incl. Odoo.sh zips)

```bash
# from an odooctl backup directory
odooctl restore acme backups/odooctl/acme-prod_20260821_101010/

# from a raw pg_dump
odooctl restore acme ~/backups/acme-prod.dump --name acme-prod

# straight from an Odoo.sh download
odooctl restore acme ~/Downloads/acme_2026-08-21.zip
```

Supported formats (detected automatically):

| Format | What it contains | Restore method |
|---|---|---|
| odooctl backup dir | `db.dump` + `filestore.tar.gz` + meta | `pg_restore` + untar |
| `.dump` file | pg_dump custom format | `pg_restore` (no filestore) |
| Odoo.sh `.zip` | `dump.sql` + `filestore/` | plain SQL replay + filestore extraction |

Behaviour:

- If a database with the target name exists you are asked to confirm overwriting it.
- Pass `--yes` / `-y` to skip that confirmation in scripts.
- Override the target name with `--name other-db` (useful for keeping the original).
- Missing filestore produces a warning - attachments will be broken but the rest
  works.
- After restore, the main internal user is automatically reset to `admin`/`admin`
  (see next section). Disable with `--no-reset-admin`.
- Add `--sanitize` to make the result safe to work on locally (see `sanitize`).

## `sanitize` - make a restored prod DB safe

Restored production databases come with live cron jobs, queued e-mails, active payment
providers and configured SMTP servers. That is how a local dev copy ends up e-mailing real
customers or charging real cards. `sanitize` executes Odoo's native neutralization engine
and scrubs personal data:

```bash
odooctl sanitize acme -d acme-prod            # or: odooctl restore acme <backup> --sanitize
```

What it does, in order:

1. Runs Odoo's native database neutralization (`odoo.modules.neutralize` and module `data/neutralize.sql` scripts) to switch payment providers and delivery carriers to test mode, activate the red top neutralization banner (`web.neutralize_banner`), website test ribbon (`website.neutralize_ribbon`), and set `database.is_neutralized = True`.
2. Pauses every scheduled action (`ir.cron`).
3. Deletes queued mails (`mail.mail` in state `outgoing`).
4. Disables outgoing mail servers (`ir.mail_server`) and fetchmail servers.
5. Clears `email`, `phone`, `mobile` and `vat` on all partners (in batches, committing
   as it goes). Raw SQL is used on purpose, so custom modules that override
   `res.partner.write()` cannot block the scrub.

Each step prints how many records it touched.

| Option | Meaning |
|---|---|
| `--names` | Also replace partner names with `Partner #id` |
| `--keep-crons` | Leave scheduled actions enabled |
| `--keep-mail` | Skip mail queue purge / server disable |

## `fix-icons` - bring missing menu icons back

Restores without a filestore break every app menu icon: Odoo stores menu icons as
attachments whose bytes live in the filestore, and without that filestore the menus
render a grey placeholder. The PNGs still exist inside the addon source folders, so
`fix-icons` re-imports them into the database:

```bash
odooctl fix-icons acme -d acme-prod
```

It checks every menu with a `web_icon`, tests whether its attachment resolves to a
real file, and for broken ones re-reads the image from
`custom_addons/<module>/static/...` (core addon paths work too). Intact icons are
left alone. Restart web or hard-refresh the browser afterwards.

`pull` and `restore` run this automatically whenever the restore had no filestore,
so icons come back without any extra step. Icons that exist neither in the database
nor in any addon folder are counted and reported as unrepairable.

## `pull` - latest backup over SSH, restored, in one command

`pull` replaces the old cycle of downloading a zip from odoo.sh in the browser and
restoring it by hand. One command fetches the newest backup, restores it, and gives
you a working `admin`/`admin` login.

One-time setup, per project:

```bash
odooctl pull acme \
    --from ssh://1234567@acme.odoo.com \
    --key ~/.ssh/id_ed25519_acme \
    -d acme_prod --save
```

Every time after that:

```bash
odooctl pull acme
```

Useful flags:

```bash
odooctl pull acme -d other_name    # restore under a different db name
odooctl pull acme --yes            # skip the overwrite prompt (scripts/CI)
odooctl pull acme --keep-download  # keep the downloaded bundle
odooctl pull acme --with-filestore # also fetch attachments (large)
odooctl pull acme --path /backup/x.sql.gz   # one-off different file
odooctl pull acme --no-sanitize    # skip automatic database neutralization
```

What a pull does, in order:

1. Connects over SSH (key auth only, no passwords) and finds the newest backup:
   `~/backup.daily`, then `~/backup.weekly`, then `~/backup.monthly` on odoo.sh
   hosts. `--path` points at a specific file.
2. Downloads the SQL dump (typically a few dozen MB compressed). The filestore is
   skipped by default; `--with-filestore` streams it too, with a progress counter.
3. Picks the target database: `-d NAME`, else the name saved with `--save`, else
   `<project>_pulled`. If that database already exists you are asked to confirm
   dropping it.
4. Stops the web container so Postgres can drop and rename without connection
   conflicts, replays the dump, and re-imports missing menu icons if restored without filestore.
5. Resets the main internal user to `admin`/`admin`.
6. Sanitizes and neutralizes the database (activating neutralization banners and safe-mode).
7. Starts web again (also on failure) and prints the project URL.

Safety behaviour worth knowing:

- The replay runs with `ON_ERROR_STOP`. A broken dump aborts loudly instead of
  leaving you a half-imported database.
- Extensions your local postgres does not have (odoo.sh pre-installs pgvector) are
  skipped with a warning, the same way a manual psql restore would.
- Interrupted downloads are reused on the next run, so a failed pull does not
  re-download the whole dump.
- Saved settings live in `~/.config/odooctl/config.json` under `"pull"`. They survive
  `discover`; delete them there to start over.

> **Odoo.sh note:** there is no public REST API for backups. SSH access is the
> supported route. Add your public key under your odoo.sh profile's *SSH Keys*
> section, and use the exact SSH string shown by the project's *SSH* button (it
> looks like `ssh 1234567@acme.odoo.com`).

## `reset-admin` - regaining admin access

Restored dumps (especially from production or shared hosting) often have unknown
logins, changed passwords, or two-factor authentication enabled. This command makes
the main internal user usable again:

```bash
odooctl reset-admin acme -d acme-prod                 # -> admin / admin
odooctl reset-admin acme -d acme-prod -l myadmin -p s3cret
odooctl reset-admin acme -d acme-prod --user-id 5     # force specific user
```

How it works:

1. Lists internal users (`share = false`) ordered by id and picks the first real one -
   skipping Odoo's system user (id 1). You'll be shown who was changed:
   ```
   [acme] user #2 (Marc Demo) was 'marc@company.example' -> now 'admin' / 'admin'
   ```
2. Writes the new login/password **through `odoo shell`**, i.e. through the ORM - so
   password hashing always matches your exact Odoo version. No fragile manual hash
   generation.
3. Disables TOTP on that account if the field exists (so no surprise 2FA prompt).

## `reset` - wiping a database

```bash
odooctl reset acme -d acme-test --yes
```

Drops the database, removes its filestore from disk, and recreates it empty
(terminates active connections first). Confirmation prompt unless `--yes`.

## `init` - bootstrapping a new project

Starting a new client/project usually means: create folder, find some older project
with the same Odoo version, copy its compose/dockerfiles, fix names and ports, start
it, restore a downloaded backup, reset the admin password. `init` does all of it:

```bash
# minimal: infer version from the backup, pick a matching template
odooctl init acme --from ~/Downloads/acme_2026-08-21.zip --parent-dir ~/work

# explicit version, fresh empty environment (no backup yet)
odooctl init acme --version 17 --parent-dir ~/work

# preview without touching anything
odooctl init acme --version 18 --dry-run
```

Step by step, `init`:

1. **Picks a template** - any registered project running the same Odoo major version
   (version inferred from `manifest.json` inside the backup zip when possible;
   override with `--version` or force a template with `--template`).
2. **Creates the folder** (`<parent-dir>/<slug>`) copying `docker-compose.yml`,
   `odoo.Dockerfile`, `postgres.Dockerfile` and `config/`, plus an empty
   `custom_addons/`.
3. **Rewrites the copied compose file**: container names become `<slug>_web` /
   `<slug>_db`, host ports are re-allocated to free ones (it checks both registered
   ports and your machine's listening ports), and enterprise-addon volume paths are
   rewritten for your home directory (`/Users/...` and `/home/...` both handled).
4. **Registers** the new project so every other command works immediately.
5. **Starts it** - and here is the trick: if the template project has already-built
   images, they are simply retagged for the new project and reused. Setup takes
   **seconds**. Only when no usable image exists does it run a real build (~10 min,
   once per Dockerfile/version per machine). Force a rebuild with `--build`.
6. **Restores** the backup into a database (named after the backup's metadata, or
   `--db`) including the filestore, then resets admin credentials to `admin`/`admin`.
7. **Waits** until Odoo actually answers on its port, then prints the URL.

Options:

| Option | Meaning |
|---|---|
| `--from FILE_OR_DIR` | Backup to restore (.zip/.dump/dir) |
| `--version, -v` | Odoo version like `18` (or inferred from backup) |
| `--template, -t` | Force a specific registered project as template |
| `--db, -d` | Database name for the restore |
| `--parent-dir, -p` | Where to create the folder (default: first scan root) |
| `--build` | Force a real image build instead of reuse |
| `--no-build` | Start without building anything |
| `--no-reset-admin` | Keep restored credentials as-is |
| `--dry-run` | Print the plan; create nothing |

> **Why image reuse matters:** Docker layer caches do *not* survive being copied
> between differently-named projects - a naive rebuild repeats every `pip install`
> layer. Reusing the built image avoids that entirely.

## `test` - running addon tests in isolation

```bash
odooctl test acme sale_approval_flow
odooctl test acme my_module --keep-db          # inspect DB after failure
odooctl test acme my_module -t /my_module.test_feature

odooctl test acme --all                        # every installable custom addon, sequentially
odooctl test acme --changed                    # only addons changed vs git HEAD
odooctl test acme --changed --since main       # ...vs another ref
odooctl test acme --all -x                     # stop at the first failing module
```

`--all` / `--changed` run modules one by one, each in its own throwaway DB, then print
a summary table (PASS/FAIL per module).

One shot, it:

1. Creates a **throwaway database** (dropping leftovers of previous runs).
2. Boots Odoo in its own container (`docker compose run`) - your running instance is
   untouched.
3. Runs `odoo -i <module> --test-enable --test-tags /<module> --stop-after-init`.
4. Saves the complete log to `backups/test_logs/<module>_<timestamp>.log`.
5. Parses unittest output and prints PASS/FAIL plus each failing test name.
6. Drops the throwaway database unless `--keep-db`.

Exit codes: `0` pass, `1` failure - safe to use in CI or git hooks.

Options:

| Option | Meaning |
|---|---|
| `--db, -d` | Throwaway DB name (default `test_<module>`) |
| `--keep-db` | Don't drop the DB afterwards |
| `--test-tags, -t` | Override Odoo's `--test-tags` selector |
| `--timeout` | Seconds before giving up |

> Writing tests: put them in `<module>/tests/test_<behavior>.py`, subclass
> `TransactionCase` (model logic) or `HttpCase` (controllers), import them from
> `<module>/tests/__init__.py`. See Odoo's official testing docs.

---

## `space` - where your disk space went

```bash
odooctl space              # every registered project
odooctl space acme         # one project
# `odooctl df` remains a compatibility alias
```

Prints global Docker totals (images, volumes, build cache, dangling layers, how
much is instantly reclaimable), then per project:

- web/db image references and sizes - images shared across projects are marked
  `(shared ×N)` so identical builds don't look triple-counted,
- **bind-mounted data dirs** measured on the host (`pg data`, `odoo data`) - this
  is where most projects keep their real gigabytes, invisible to `docker system df`,
- named volumes with sizes (`acme_pgdata`, ...) if the project uses any,
- `backups/odooctl/` size and snapshot count,
- `backups/test_logs/` size and file count,
- a per-project total, plus an overall summary.

It also lists **untracked tagged images** (e.g. builds left behind by renamed
projects) with their sizes.

## `gc` - reclaiming wasted space

Docker dev setups leak silently: dangling image layers after every rebuild,
build cache, `test_*` databases, filestores of dropped databases that Odoo never
removes, old backups and test logs. `gc` finds them all - **and only touches
projects registered in odooctl**:

```bash
odooctl gc                       # dry run: shows the plan + sizes, changes nothing
odooctl gc --apply               # execute the plan
odooctl gc acme --apply          # limit to one project
odooctl gc --apply --keep-backups 5 --keep-logs 50
```

What it cleans, per project:

| Item | Action |
|---|---|
| Dangling image layers | `docker image prune` |
| Build cache | `docker builder prune` (next builds re-download/recompile) |
| `test_*` databases | DROP database + delete its filestore |
| Orphan filestores | filestore dirs whose DB no longer exists are deleted |
| Old backup snapshots | newest `--keep-backups` per db survive (default 3) |
| Old test logs | newest `--keep-logs` files survive (default 20) |
| Orphan anonymous volumes | hash-named volumes no container uses anymore (auto-created by postgres/odoo images) |
| Untracked tagged images | only with `--stale-images`: images no registered project uses (old renamed-project builds). Base images may re-download on a later build |

Live checks (test DBs, orphan filestores) need that project's containers running;
stopped projects still get host-side cleanup (backups, logs).

### The nuclear option

```bash
odooctl gc acme --deep
```

Wipes **all named volumes of one project** - every database and every filestore -
after a loud confirmation. Bind mounts (addons, config, backups) are untouched.
Use when a pgdata volume has bloated beyond recovery; afterwards `odooctl up`
gives you a factory-fresh environment, and `restore` brings your data back.
The former `odooctl gc-deep acme` spelling remains available for compatibility but
is hidden from the main help.

> **macOS note:** after big cleanups, Docker Desktop's Linux VM keeps the disk
> space it allocated until restarted (or trimmed automatically on recent
> versions). Restarting Docker Desktop releases it back to the host.
> **Layer sharing:** projects created by `init` from the same template share the
> same image tags and Docker layer cache - a new project costs megabytes, not
> gigabytes, as long as you don't modify its Dockerfiles differently.

## `remove` - deleting a project

```bash
odooctl remove newclient                          # stop containers + unregister (files kept)
odooctl remove newclient --images                 # ...and remove images not shared with other projects
odooctl remove newclient --purge-folder           # ...and DELETE the whole folder from disk
```

What happens, in order:

1. Containers are stopped and removed together with their named volumes
   (`docker compose down -v`).
2. With `--images`: each project image is removed via `docker rmi` - **unless**
   another registered project uses the identical image ID (e.g. projects created
   from the same template); shared images are kept and reported.
3. With `--purge-folder`: after a separate loud confirmation, the project folder
   itself is deleted - source code, `data/`, backups, everything. Without this
   flag your files stay untouched on disk.
4. The project is unregistered. Re-running `odooctl discover` will no longer see it.

---

## Configuration file reference

Location: `~/.config/odooctl/config.json` (both platforms). Override the whole
location with the `ODOOCTL_HOME` environment variable (its value replaces
`~/.config/odooctl`).

```jsonc
{
  "roots": ["/home/you/work"],          // saved scan roots (cwd is scanned but never saved)
  "projects": {
    "acme": {
      "path": "/home/you/work/acme",
      "compose_file": "/home/you/work/acme/docker-compose.yml",
      "services": { "web": "web", "db": "db" },
      "container_names": { "web": "acme_web", "db": "acme_db" },
      "ports": { "http": 8070, "longpolling": 8071, "debugpy": 8072, "pg_postgres": 5470 },
      "db_user": "odoo",
      "custom_addons": "/home/you/work/acme/custom_addons",
      "images": { "web": null, "db": null }   // explicit compose image:, usually null
    }
  }
}
```

You normally never edit this file - `discover` maintains it, `init` adds entries,
`unregister` happens implicitly when needed. Safe to delete the file entirely;
everything is rediscovered on the next `discover`.

## macOS vs Linux notes

| Topic | macOS | Linux |
|---|---|---|
| Docker runtime | Docker Desktop | Docker Engine (systemd) or Docker Desktop |
| Start Docker | Launch Docker Desktop app | `sudo systemctl start docker` |
| Daemon permissions | n/a | add yourself to the `docker` group, then re-login |
| Config location | `~/.config/odooctl/config.json` | same |
| Install commands | identical (pipx / uv / venv) | identical |
| Enterprise path rewrite during `init` | `/Users/<you>` | `/home/<you>` (automatic either way) |
| Disk reclaim after `gc --apply` | restart Docker Desktop (its VM releases space lazily) | immediate |

Everything else - commands, flags, behaviour, output - is identical on both systems.

## Troubleshooting

**"Docker daemon not reachable"**
Docker isn't running. macOS: launch Docker Desktop. Linux: `sudo systemctl start
docker`. Then retry.

**"No Odoo projects found"**
Read the report printed under the error. A root marked `missing` does not exist on this
machine; drop it with `odooctl discover --forget-root PATH`. A root with `0 compose files`
does not contain your projects within three folders; add the folder directly above them
with `odooctl discover --root ~/my-work`. A compose file listed under "rejected" was seen
but did not look like Odoo; the reason next to it says what is missing (an `odoo` service,
a Postgres service, valid YAML). Running `odooctl discover` from inside the project folder
also works, since the current directory is always scanned.

**`FATAL: database "<user>" does not exist`**
You connected to psql without `-d`. PostgreSQL falls back to a database named after
the user. Always pass `odooctl psql <project> -d <database>`.

**`Error: Multiple databases: ...`**
`addons`, `shell` and `diff` need to know which DB to act on. Pass `-d <database>`.

**`pull` says "Permission denied (publickey)"**
Your SSH key is not registered with the host. For odoo.sh: add your public key under
your profile's *SSH Keys* section, then test with the exact string from the project's
*SSH* button: `ssh <user>@<host> 'echo ok'`. `pull` uses key auth only, never
passwords.

**`pull` says "No backup (*.sql.gz) found on remote"**
It scans `~/backup.daily`, `~/backup.weekly` and `~/backup.monthly` on the remote
host. Your host stores backups elsewhere? Point at the file directly:
`odooctl pull <project> --path /path/to/backup.sql.gz`.

**A restore fails with `could not open extension control file ...`**
The dump uses a Postgres extension (e.g. pgvector) your local db image does not ship.
`pull` and `restore` skip missing extensions automatically and warn. If a table
actually needs the extension, add it to your `postgres.Dockerfile` and rebuild.

**First `init` takes ~10 minutes**
No prebuilt image existed for that Odoo version/Dockerfile yet, so a real build ran.
It won't happen again on that machine - later inits reuse the image in seconds. Use
`--build` only after intentionally changing a Dockerfile.

**Ports in `init` look odd (8074 instead of 8070)**
Port allocation skips anything already used by registered projects or occupied on
your system, bumping upward until free.

**A restored DB asks for 2FA**
Shouldn't happen anymore - `reset-admin` disables TOTP on the reset user. If it does,
run `odooctl reset-admin <project> -d <db>` again.

**Docker takes tons of disk space**
Run `odooctl space` to see where it goes, then `odooctl gc --apply`. Typical culprits:
dangling layers from rebuilds, build cache, leftover `test_*` databases and the
filestores of databases deleted long ago. If a `pgdata` volume itself has grown
huge (Postgres never shrinks files), use `odooctl gc <project> --deep` after taking
a backup.

## Development

```bash
git clone https://github.com/Hesham1902/odooctl && cd odooctl
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

CI runs lint + tests on Python 3.10 / 3.12 / 3.14 on every push and PR. Please add
tests for new features; keep the suite green.

The Click layer is organized by user-facing family under `src/odooctl/commands/`.
Keep handlers thin and put reusable behavior in the existing domain modules such as
`compose.py`, `restore.py`, `testing.py` and `space.py`.
