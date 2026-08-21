# odooctl

One CLI for all your local Odoo docker environments.

If you maintain more than one Odoo project locally, you juggle different container
names, ports, databases and long `docker compose exec ...` incantations for each one.
`odooctl` discovers your projects once, stores them in a registry, and gives you one
uniform command surface - from starting/stopping environments to restoring Odoo.sh
backups, bootstrapping brand-new projects in seconds, and running addon tests in
throwaway databases.

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
7. [`logs` and `url` - following output, opening the app](#logs-and-url---following-output-opening-the-app)
8. [`psql` - direct database access](#psql---direct-database-access)
9. [`addons` - inspecting custom addons](#addons---inspecting-custom-addons)
10. [`upgrade` - upgrading one module safely](#upgrade---upgrading-one-module-safely)
11. [`backup` - snapshotting a database](#backup---snapshotting-a-database)
12. [`restore` - restoring backups (incl. Odoo.sh zips)](#restore---restoring-backups-incl-odoosh-zips)
13. [`reset-admin` - regaining admin access](#reset-admin---regaining-admin-access)
14. [`reset` - wiping a database](#reset---wiping-a-database)
15. [`init` - bootstrapping a new project](#init---bootstrapping-a-new-project)
16. [`test` - running addon tests in isolation](#test---running-addon-tests-in-isolation)

**Reference**

17. [Configuration file reference](#configuration-file-reference)
18. [macOS vs Linux notes](#macos-vs-linux-notes)
19. [Troubleshooting](#troubleshooting)
20. [Development](#development)

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
odooctl discover                  # scan default roots
odooctl discover --root ~/code    # scan additional folder(s)
```

`discover` walks your work folders looking for `docker-compose.yml` files whose
services look like an Odoo setup (an Odoo web service + a Postgres service). Every hit
is registered under a slug derived from its web container's name, e.g. container
`acme_web` becomes project **`acme`**. The result looks like:

```
Found 3 project(s):
acme           /home/you/work/acme              http:8070   pg:5470
beta           /home/you/work/beta              http:8071   pg:5455
gamma          /home/you/work/gamma             http:8056   pg:5456
```

Nothing is started or modified by discovery - it only reads files.

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
- **Scan roots**: the folders `discover` searches. Defaults include common workspace
  locations plus your current directory when nothing else is configured.
- **Image naming**: Docker Compose names built images `<folder>-<service>`
  (folder `acme` → image `acme-web`). `odooctl init` exploits this to reuse existing
  images instantly; see the [`init`](#init---bootstrapping-a-new-project) section.
- **Safety defaults**: destructive operations (`restore`, `reset`) ask for
  confirmation before dropping data; `test` always runs against a throwaway database,
  never yours.

---

# Command reference

| Command | Purpose |
|---|---|
| `discover` | (Re)scan folders for Odoo projects |
| `projects` | List registered projects |
| `status` | Containers + databases, all projects or one |
| `up` | Start a project's containers |
| `down` | Stop a project's containers |
| `restart` | Restart one service |
| `logs` | Show or stream logs |
| `url` | Open the project in a browser |
| `psql` | Interactive SQL session |
| `addons` | List custom addons (+ install state) |
| `upgrade` | Upgrade one addon in one database |
| `backup` | Dump a database + filestore |
| `restore` | Restore a backup (dir / dump / Odoo.sh zip) |
| `reset-admin` | Reset the main user's login/password |
| `reset` | Drop and recreate a database |
| `init` | Bootstrap a new project from an existing one |
| `test` | Run addon tests in a disposable DB |

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

## `logs` and `url` - following output, opening the app

```bash
odooctl logs acme             # last 100 lines of the web container
odooctl logs acme -f          # stream live (Ctrl-C to stop)
odooctl logs acme -t 500      # last 500 lines
odooctl logs acme --service db
```

```bash
odooctl url acme              # prints http://localhost:<port> and opens browser
```

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

## `upgrade` - upgrading one module safely

```bash
odooctl upgrade acme sale_approval_flow -d acme-prod
odooctl upgrade acme my_module -d acme-prod --keep-stopped
```

What it does, in order:

1. Checks whether the web service is currently running.
2. If yes, stops it (a running instance would conflict with the upgrade worker).
3. Runs `odoo -u <module> --stop-after-init` in a throwaway container - the module's
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
- Override the target name with `--name other-db` (useful for keeping the original).
- Missing filestore produces a warning - attachments will be broken but the rest
  works.
- After restore, the main internal user is automatically reset to `admin`/`admin`
  (see next section). Disable with `--no-reset-admin`.

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

Drops the database and recreates it empty (terminates active connections first).
Confirmation prompt unless `--yes`. Never touches the filestore - combine with a fresh
install if you need full cleanliness.

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
```

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

## Configuration file reference

Location: `~/.config/odooctl/config.json` (both platforms). Override the whole
location with the `ODOOCTL_HOME` environment variable (its value replaces
`~/.config/odooctl`).

```jsonc
{
  "roots": ["/home/you/work"],          // where discover() scans
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

Everything else - commands, flags, behaviour, output - is identical on both systems.

## Troubleshooting

**"Docker daemon not reachable"**
Docker isn't running. macOS: launch Docker Desktop. Linux: `sudo systemctl start
docker`. Then retry.

**"No Odoo projects found under the scan roots"**
Your projects aren't under the default roots. Point discovery at them:
`odooctl discover --root ~/my-work`.

**`FATAL: database "<user>" does not exist`**
You connected to psql without `-d`. PostgreSQL falls back to a database named after
the user. Always pass `odooctl psql <project> -d <database>`.

**`Error: Multiple databases: ...`**
`addons` needs to know which DB to read module state from. Pass `-d <database>`.

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

## Development

```bash
git clone https://github.com/Hesham1902/odooctl && cd odooctl
pip install -e ".[dev]"
ruff check src tests
pytest -q
```

CI runs lint + tests on Python 3.10 / 3.12 / 3.14 on every push and PR. Please add
tests for new features; keep the suite green.
