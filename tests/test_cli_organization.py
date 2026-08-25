from click.testing import CliRunner

from odooctl import cli, registry


def _register(slug, tmp_path):
    project_dir = tmp_path / slug
    project_dir.mkdir()
    registry.register(
        slug,
        {
            "compose_file": str(project_dir / "docker-compose.yml"),
            "path": str(project_dir),
            "services": {"web": "web", "db": "db"},
            "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
            "ports": {"http": 8069},
            "db_user": "odoo",
        },
    )
    return registry.get_projects()[slug]


def test_root_help_groups_commands_and_shows_compatibility_aliases():
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0, result.output
    for section in ("Project management:", "Runtime:", "Development:", "Database:", "Storage:"):
        assert section in result.output
    assert "open (url)" in result.output
    assert "space (df)" in result.output
    assert "gc-deep" not in result.output


def test_old_aliases_still_resolve_to_the_new_commands():
    runner = CliRunner()

    open_help = runner.invoke(cli.main, ["url", "--help"])
    space_help = runner.invoke(cli.main, ["df", "--help"])

    assert open_help.exit_code == 0
    assert "Open the project" in open_help.output
    assert space_help.exit_code == 0
    assert "Docker disk usage" in space_help.output


def test_common_errors_include_an_actionable_hint(monkeypatch):
    runner = CliRunner()

    missing_project = runner.invoke(cli.main, ["open", "missing"])
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: False)
    missing_docker = runner.invoke(cli.main, ["status"])

    assert missing_project.exit_code != 0
    assert "Hint: run `odooctl projects`" in missing_project.output
    assert missing_docker.exit_code != 0
    assert "Hint: verify Docker with `docker info`" in missing_docker.output


def test_destructive_database_commands_share_yes_short_option():
    runner = CliRunner()

    restore_help = runner.invoke(cli.main, ["restore", "--help"])
    reset_help = runner.invoke(cli.main, ["reset", "--help"])

    assert "-y, --yes" in restore_help.output
    assert "-y, --yes" in reset_help.output


def test_debug_prints_named_and_total_timings(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(
        cli.compose,
        "ps",
        lambda path: [{"Service": "db", "Name": "acme_db", "State": "running"}],
    )
    monkeypatch.setattr(cli.compose, "databases", lambda *args, **kwargs: ["prod"])

    result = CliRunner().invoke(cli.main, ["--debug", "status", "acme"])

    assert result.exit_code == 0, result.output
    assert "[debug] timings" in result.output
    assert "docker availability" in result.output
    assert "project status" in result.output
    assert "total" in result.output


def test_status_reuses_the_compose_state_probe(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(
        cli.compose,
        "ps",
        lambda path: [{"Service": "db", "Name": "acme_db", "State": "running"}],
    )
    calls = []

    def fake_databases(path, user, check_running=True):
        calls.append(check_running)
        return ["prod"]

    monkeypatch.setattr(cli.compose, "databases", fake_databases)

    result = CliRunner().invoke(cli.main, ["status", "acme"])

    assert result.exit_code == 0, result.output
    assert calls == [False]
    assert "databases: prod" in result.output


def test_gc_deep_is_an_option_and_legacy_command_remains_compatible(tmp_path, monkeypatch):
    _register("acme", tmp_path)
    monkeypatch.setattr(cli.compose, "daemon_available", lambda: True)
    monkeypatch.setattr(cli.space, "project_volume_sizes", lambda slug: {"acme_data": 100})
    executed = []
    monkeypatch.setattr(cli.space, "execute", lambda items: executed.extend(items) or 100)

    result = CliRunner().invoke(cli.main, ["gc", "acme", "--deep", "--yes"])
    legacy = CliRunner().invoke(cli.main, ["gc-deep", "acme", "--yes"])

    assert result.exit_code == 0, result.output
    assert legacy.exit_code == 0, legacy.output
    assert len(executed) == 2
    assert all(item.kind == "deep-volumes" for item in executed)
