import subprocess
from pathlib import Path

from click.testing import CliRunner
from conftest import write_compose

from odooctl import cli, onboarding, registry
from odooctl.commands import projects as projects_mod
from odooctl.registry import parse_compose


class _FakeProc:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


def _register_template(tmp_path, slug="tmpl18", version="18"):
    d = write_compose(
        tmp_path / slug,
        version=version,
        host_prefix=str(Path.home()),
        containers=(f"{slug}_web", f"{slug}_db"),
    )
    found_slug, entry = parse_compose(d / "docker-compose.yml")
    registry.register(found_slug, entry)
    return found_slug, entry


def _no_op_full_boot(monkeypatch, order=None):
    """Wire up the docker/network side of init as harmless no-ops so the CLI
    can run its full non-dry-run path in tests."""
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.compose, "find_built_image", lambda entry, role: None)

    def fake_compose_run(path, *args, **kwargs):
        if order is not None:
            order.append(("compose.run", args))
        return None

    monkeypatch.setattr(cli.compose, "run", fake_compose_run)
    monkeypatch.setattr(projects_mod, "wait_http", lambda port, timeout=120: True)


def _fake_git_clone(order=None, manifest_at_root=True):
    def _run(cmd, *args, **kwargs):
        assert cmd[:2] == ["git", "clone"]
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        if manifest_at_root:
            (dest / "__manifest__.py").write_text("{'name': 'x'}")
        if order is not None:
            order.append(("git.clone", str(dest)))
        return _FakeProc(0)

    return _run


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------


def test_init_help_documents_guided_mode_and_addon_repo():
    result = CliRunner().invoke(cli.main, ["init", "--help"])
    assert result.exit_code == 0, result.output
    assert "--addon-repo" in result.output
    assert "guided" in result.output.lower() or "interactive" in result.output.lower()
    assert "odooctl init acme --version 18" in result.output


# ---------------------------------------------------------------------------
# Non-TTY without NAME: clear error, no hang
# ---------------------------------------------------------------------------


def test_init_without_name_and_non_tty_fails_clearly(monkeypatch):
    monkeypatch.setattr(onboarding, "stdin_is_interactive", lambda: False)

    result = CliRunner().invoke(cli.main, ["init"])

    assert result.exit_code != 0
    assert "needs a NAME" in result.output
    assert "odooctl init acme --version 18" in result.output
    assert "guided setup" in result.output


# ---------------------------------------------------------------------------
# Preflight: fails before any mutation
# ---------------------------------------------------------------------------


def test_init_unknown_template_gives_hint_and_touches_nothing(tmp_path, monkeypatch):
    _register_template(tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    result = CliRunner().invoke(
        cli.main, ["init", "acme", "--template", "does-not-exist", "--parent-dir", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "Unknown template" in result.output
    assert "Hint:" in result.output
    assert "Nothing was changed." in result.output
    assert not (tmp_path / "acme").exists()


def test_init_destination_conflict_gives_hint(tmp_path, monkeypatch):
    slug, _ = _register_template(tmp_path, version="18")
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    result = CliRunner().invoke(
        cli.main, ["init", "acme", "--version", "18", "--parent-dir", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "odooctl remove" in result.output
    assert registry.get_projects().get("acme") is None


def test_init_rejects_corrupt_backup_zip_before_mutation(tmp_path, monkeypatch):
    _register_template(tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    bad_zip = tmp_path / "backup.zip"
    bad_zip.write_bytes(b"not a zip")

    result = CliRunner().invoke(
        cli.main,
        ["init", "acme", "--version", "18", "--parent-dir", str(tmp_path), "--from", str(bad_zip)],
    )

    assert result.exit_code != 0
    assert "isn't a valid zip archive" in result.output
    assert not (tmp_path / "acme").exists()


def test_init_missing_git_for_addon_repo_fails_before_mutation(tmp_path, monkeypatch):
    _register_template(tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(projects_mod.shutil, "which", lambda name: None)

    result = CliRunner().invoke(
        cli.main,
        [
            "init",
            "acme",
            "--version",
            "18",
            "--parent-dir",
            str(tmp_path),
            "--addon-repo",
            "https://example.com/oca/queue.git",
        ],
    )

    assert result.exit_code != 0
    assert "git is required" in result.output
    assert not (tmp_path / "acme").exists()


def test_init_bad_addon_repo_value_rejected(tmp_path, monkeypatch):
    _register_template(tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    result = CliRunner().invoke(
        cli.main,
        ["init", "acme", "--version", "18", "--parent-dir", str(tmp_path), "--addon-repo", "#18.0"],
    )

    assert result.exit_code != 0
    assert "missing a repository URL" in result.output


# ---------------------------------------------------------------------------
# --dry-run: plan only, no side effects
# ---------------------------------------------------------------------------


def test_init_dry_run_shows_plan_and_addon_repos_without_side_effects(tmp_path, monkeypatch):
    _register_template(tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    result = CliRunner().invoke(
        cli.main,
        [
            "init",
            "acme",
            "--version",
            "18",
            "--parent-dir",
            str(tmp_path),
            "--addon-repo",
            "https://example.com/oca/queue.git#18.0",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "dry run - nothing created" in result.output
    assert "addon repos" in result.output
    assert "https://example.com/oca/queue.git#18.0" in result.output
    assert not (tmp_path / "acme").exists()
    assert registry.get_projects().get("acme") is None


# ---------------------------------------------------------------------------
# Full non-interactive flow: addon repos cloned before `up`
# ---------------------------------------------------------------------------


def test_init_non_interactive_clones_addon_repo_before_starting(tmp_path, monkeypatch):
    _register_template(tmp_path)
    order = []
    _no_op_full_boot(monkeypatch, order=order)
    monkeypatch.setattr(subprocess, "run", _fake_git_clone(order=order))

    result = CliRunner().invoke(
        cli.main,
        [
            "init",
            "acme",
            "--version",
            "18",
            "--parent-dir",
            str(tmp_path),
            "--addon-repo",
            "https://example.com/acme/my_addon.git",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "module 'my-addon' ready" in result.output
    assert (tmp_path / "acme" / "custom_addons" / "my-addon" / "__manifest__.py").is_file()
    kinds = [k for k, _ in order]
    assert kinds.index("git.clone") < kinds.index("compose.run")


def test_init_non_interactive_collection_repo_goes_directly_into_custom_addons(tmp_path, monkeypatch):
    _register_template(tmp_path)
    _no_op_full_boot(monkeypatch)
    monkeypatch.setattr(subprocess, "run", _fake_git_clone(manifest_at_root=False))

    def clone_with_children(cmd, *a, **k):
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        for mod in ("queue_job", "queue_job_cron"):
            (dest / mod).mkdir()
            (dest / mod / "__manifest__.py").write_text("{}")
        return _FakeProc(0)

    monkeypatch.setattr(subprocess, "run", clone_with_children)

    result = CliRunner().invoke(
        cli.main,
        ["init", "acme", "--version", "18", "--parent-dir", str(tmp_path), "--addon-repo",
         "https://example.com/oca/queue.git"],
    )

    assert result.exit_code == 0, result.output
    assert "collection repo 'queue' ready directly in custom_addons/" in result.output
    assert (tmp_path / "acme" / "custom_addons" / "queue_job" / "__manifest__.py").is_file()
    assert not (tmp_path / "acme" / "custom_addons" / "queue").exists()


def test_init_non_interactive_pulls_remote_backup_after_starting(tmp_path, monkeypatch):
    _register_template(tmp_path)
    order = []
    _no_op_full_boot(monkeypatch, order=order)
    pulled = []

    def fake_run_pull(slug, project_entry, options):
        pulled.append((slug, project_entry, options))
        order.append(("pull", slug))

    monkeypatch.setattr(projects_mod.pull_workflow, "run_pull", fake_run_pull)

    result = CliRunner().invoke(
        cli.main,
        [
            "init",
            "acme",
            "--version",
            "18",
            "--parent-dir",
            str(tmp_path),
            "--pull-from",
            "ssh://1234567@acme.odoo.com",
            "--pull-with-filestore",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "remote pull" in result.output
    assert pulled[0][0] == "acme"
    assert pulled[0][2].ssh_target == "ssh://1234567@acme.odoo.com"
    assert pulled[0][2].with_filestore is True
    assert next(i for i, item in enumerate(order) if item[0] == "compose.run") < order.index(("pull", "acme"))


def test_init_interactive_wizard_can_choose_remote_pull(tmp_path, monkeypatch):
    _register_template(tmp_path, slug="tmpl18", version="18")
    monkeypatch.setattr(onboarding, "stdin_is_interactive", lambda: True)
    _no_op_full_boot(monkeypatch)
    pulled = []

    def fake_run_pull(slug, project_entry, options):
        pulled.append((slug, options))

    monkeypatch.setattr(projects_mod.pull_workflow, "run_pull", fake_run_pull)
    answers = "\n".join(
        [
            "acme",
            str(tmp_path),
            "",  # local backup: none
            "y",  # use remote pull
            "ssh://1234567@acme.odoo.com",
            "",  # newest remote backup
            "",  # default SSH key
            "n",  # no filestore
            "y",  # reset admin
            "y",  # sanitize
            "y",  # repair icons
            "n",  # do not keep bundle
            "n",  # do not overwrite
            "y",  # remember pull settings
            "",  # default database
            "n",  # no addon repo
            "y",  # confirm creation
            "",
        ]
    )

    result = CliRunner().invoke(cli.main, ["init"], input=answers)

    assert result.exit_code == 0, result.output
    assert "remote pull" in result.output
    assert pulled[0][0] == "acme"
    assert pulled[0][1].ssh_target == "ssh://1234567@acme.odoo.com"
    assert pulled[0][1].save_settings is True


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------


def test_init_interactive_wizard_fills_gaps_shows_plan_and_confirms(tmp_path, monkeypatch):
    _register_template(tmp_path, slug="tmpl18", version="18")
    order = []
    monkeypatch.setattr(onboarding, "stdin_is_interactive", lambda: True)
    _no_op_full_boot(monkeypatch, order=order)
    monkeypatch.setattr(subprocess, "run", _fake_git_clone(order=order))

    answers = "\n".join(
        [
            "acme",  # project name
            str(tmp_path),  # parent directory
            "",  # backup: none
            "n",  # do not pull a remote backup
            "y",  # add an addon repo
            "https://example.com/oca/module.git",  # repo url
            "",  # ref: default branch
            "n",  # no more repos
            "y",  # confirm creation
            "",
        ]
    )

    result = CliRunner().invoke(cli.main, ["init"], input=answers)

    assert result.exit_code == 0, result.output
    assert "guided setup" in result.output
    assert "Create this project?" in result.output
    assert "new project   : acme" in result.output
    assert (tmp_path / "acme" / "custom_addons" / "module" / "__manifest__.py").is_file()
    assert registry.get_projects().get("acme") is not None


def test_init_interactive_wizard_aborts_without_creating_when_declined(tmp_path, monkeypatch):
    _register_template(tmp_path, slug="tmpl18", version="18")
    monkeypatch.setattr(onboarding, "stdin_is_interactive", lambda: True)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)

    answers = "\n".join(
        [
            "acme",
            str(tmp_path),
            "",  # backup: none
            "n",  # do not pull a remote backup
            "n",  # no addon repo
            "n",  # do not confirm creation
            "",
        ]
    )

    result = CliRunner().invoke(cli.main, ["init"], input=answers)

    assert result.exit_code != 0
    assert not (tmp_path / "acme").exists()
    assert registry.get_projects().get("acme") is None
