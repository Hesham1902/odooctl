from pathlib import Path

from click.testing import CliRunner

from odooctl import cli, deps, registry


def _register(slug, tmp_path):
    d = tmp_path / slug
    d.mkdir()
    addons = d / "custom_addons"
    addons.mkdir()
    registry.register(
        slug,
        {
            "compose_file": str(d / "docker-compose.yml"),
            "path": str(d),
            "services": {"web": "web", "db": "db"},
            "container_names": {"web": f"{slug}_web", "db": f"{slug}_db"},
            "ports": {"http": 8069},
            "custom_addons": str(addons),
            "db_user": "odoo",
        },
    )
    return registry.get_projects()[slug]


def _make_module(addons_dir, name, depends=(), version=None):
    mod = Path(addons_dir) / name
    mod.mkdir(parents=True)
    mf = f"{{'name': '{name}', 'depends': {list(depends)!r}"
    if version:
        mf += f", 'version': '{version}'"
    (mod / "__manifest__.py").write_text(mf + "}")


def test_transitive_deps():
    graph = {"a": ["b", "mail"], "b": ["c"], "c": []}
    assert deps.transitive_deps(graph, "a") == {"b", "c", "mail"}


def test_dependents_inverts():
    graph = {"a": ["base"], "b": ["base"]}
    assert deps.dependents(graph) == {"base": {"a", "b"}}


def test_find_cycle_detects():
    graph = {"a": ["b"], "b": ["c"], "c": ["a"]}
    cycle = deps.find_cycle(graph, "a")
    assert cycle and cycle[0] == cycle[-1] == "a"


def test_find_cycle_none():
    assert deps.find_cycle({"a": ["b"], "b": []}, "a") is None


def test_deps_cli_tree_and_reverse(tmp_path):
    entry = _register("acme", tmp_path)
    addons = entry["custom_addons"]
    _make_module(addons, "top_mod", depends=["helper_mod", "sale"], version="18.0.1.0.0")
    _make_module(addons, "helper_mod", depends=["sale"])
    _make_module(addons, "other_user", depends=["top_mod"])

    result = CliRunner().invoke(cli.main, ["deps", "acme", "top_mod"])
    assert result.exit_code == 0, result.output
    assert "`-- sale [external]" in result.output
    assert "-- helper_mod" in result.output
    assert "transitive: 1 custom, 1 external" in result.output
    assert "required by: other_user" in result.output
    assert "CYCLE" not in result.output


def test_deps_cli_reports_cycle(tmp_path):
    entry = _register("acme", tmp_path)
    addons = entry["custom_addons"]
    _make_module(addons, "loop_a", depends=["loop_b"])
    _make_module(addons, "loop_b", depends=["loop_a"])

    result = CliRunner().invoke(cli.main, ["deps", "acme", "loop_a"])
    assert result.exit_code == 0, result.output
    assert "CYCLE DETECTED" in result.output


def test_deps_cli_unknown_module_lists_available(tmp_path):
    entry = _register("acme", tmp_path)
    _make_module(entry["custom_addons"], "present")

    result = CliRunner().invoke(cli.main, ["deps", "acme", "missing"])
    assert result.exit_code != 0
    assert "present" in result.output
