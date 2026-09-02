from pathlib import Path

from conftest import write_compose

from odooctl import registry
from odooctl.registry import discover, parse_compose


def test_parse_compose_extracts_everything(tmp_path):
    d = write_compose(tmp_path / "proj", version="16")
    (d / "custom_addons").mkdir()

    parsed = parse_compose(d / "docker-compose.yml")

    assert parsed is not None
    slug, entry = parsed
    assert slug == "acme"
    assert entry["services"] == {"web": "web", "db": "db"}
    assert entry["container_names"] == {"web": "acme_web", "db": "acme_db"}
    assert entry["ports"]["http"] == 8056
    assert entry["ports"]["longpolling"] == 8072
    assert entry["ports"]["debugpy"] == 8777
    assert entry["ports"]["pg_postgres"] == 5456
    assert entry["db_user"] == "odoo"
    assert entry["custom_addons"].endswith("custom_addons")


def test_parse_compose_rejects_non_odoo(tmp_path):
    d = tmp_path / "webapp"
    d.mkdir()
    (d / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx\n    ports: ['80:80']\n")
    assert parse_compose(d / "docker-compose.yml") is None


def test_detect_version_from_volume_path(tmp_path):
    d = write_compose(tmp_path / "p1", version="16")
    entry = parse_compose(d / "docker-compose.yml")[1]
    assert registry.detect_version(entry) == "16.0"


def test_detect_version_from_dockerfile(tmp_path):
    d = write_compose(tmp_path / "p2", include_enterprise=False)
    (d / "odoo.Dockerfile").write_text("FROM odoo:18.0\nRUN pip install x\n")
    entry = parse_compose(d / "docker-compose.yml")[1]
    assert registry.detect_version(entry) == "18.0"


def test_detect_version_unknown(tmp_path):
    d = write_compose(tmp_path / "p3", include_enterprise=False)
    entry = parse_compose(d / "docker-compose.yml")[1]
    assert registry.detect_version(entry) is None


def test_normalize_version_variants():
    assert registry.normalize_version("18") == "18.0"
    assert registry.normalize_version("16.0") == "16.0"
    assert registry.normalize_version(17) == "17.0"
    assert registry.normalize_version("v18") is None


def _seed_two_projects():
    registry.register("alpha", {"path": "/a", "compose_file": "/a/docker-compose.yml"})
    registry.register("beta", {"path": "/b", "compose_file": "/b/docker-compose.yml"})


def test_resolve_exact_and_prefix():
    _seed_two_projects()
    slug, _ = registry.resolve("alpha")
    assert slug == "alpha"
    slug, _ = registry.resolve("alp")
    assert slug == "alpha"
    slug, _ = registry.resolve("bet")
    assert slug == "beta"


def test_resolve_unknown_raises_with_hint():
    _seed_two_projects()
    try:
        registry.resolve("gamma")
        raise AssertionError("expected KeyError")
    except KeyError as exc:
        assert "alpha" in str(exc)
        assert "beta" in str(exc)


def test_register_unregister_roundtrip():
    registry.register("x", {"path": "/x"})
    assert "x" in registry.get_projects()
    registry.unregister("x")
    assert "x" not in registry.get_projects()


def test_discover_finds_projects_skips_noise(tmp_path):
    good = write_compose(tmp_path / "work" / "good", version="18")
    deep = tmp_path / "work" / "a" / "b" / "c" / "d"
    write_compose(deep, version="16")
    write_compose(tmp_path / "work" / ".hidden", version="16")

    found = discover([tmp_path / "work"])

    paths = [entry["path"] for entry in found.values()]
    assert str(good) in paths or any(p.endswith("/good") for p in paths)
    assert all("good" in p for p in paths)
    assert not any(".hidden" in p for p in paths)
    assert not any("/a/b/c/d" in p for p in paths)


# --- compose filename variants -------------------------------------------------------------


def test_discover_accepts_every_compose_filename(tmp_path):
    names = ["compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"]
    for i, name in enumerate(names):
        write_compose(tmp_path / "work" / f"p{i}", containers=(f"p{i}_web", f"p{i}_db"), filename=name)

    found = discover([tmp_path / "work"])

    assert sorted(found) == ["p0", "p1", "p2", "p3"]
    assert {Path(e["compose_file"]).name for e in found.values()} == set(names)


def test_discover_one_project_per_dir_using_docker_precedence(tmp_path):
    d = write_compose(tmp_path / "work" / "proj", filename="docker-compose.yml")
    write_compose(d, filename="compose.yaml")

    found = discover([tmp_path / "work"])

    assert list(found) == ["acme"]
    assert Path(found["acme"]["compose_file"]).name == "compose.yaml"


# --- walking rules ---------------------------------------------------------------------------


def test_discover_root_under_hidden_ancestor_still_works(tmp_path):
    root = tmp_path / ".cache" / "work"
    write_compose(root / "proj")

    found = discover([root])

    assert "acme" in found


def test_scan_follows_symlinked_project_dirs(tmp_path):
    real = write_compose(tmp_path / "elsewhere" / "proj")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "link").symlink_to(real, target_is_directory=True)

    found, report = registry.scan([tmp_path / "work"])

    assert "acme" in found
    assert report.rejected == []


def test_scan_reports_missing_root_and_counts(tmp_path):
    write_compose(tmp_path / "work" / "proj")

    found, report = registry.scan([tmp_path / "work", tmp_path / "nope"])

    work = str((tmp_path / "work").resolve())
    nope = str((tmp_path / "nope").resolve())
    assert report.roots[work] == 1
    assert report.roots[nope] is None
    assert report.missing_roots == [nope]
    assert report.compose_files_seen == 1
    assert "acme" in found


def test_scan_hidden_dir_is_neither_found_nor_reported(tmp_path):
    write_compose(tmp_path / "work" / ".hidden")
    write_compose(tmp_path / "work" / "node_modules" / "x")

    found, report = registry.scan([tmp_path / "work"])

    assert found == {}
    assert report.rejected == []
    assert report.roots[str((tmp_path / "work").resolve())] == 0


def test_scan_reports_too_deep_project(tmp_path):
    write_compose(tmp_path / "work" / "a" / "b" / "c" / "d")
    write_compose(tmp_path / "work" / "a" / "b" / "c", containers=("ok_web", "ok_db"))

    found, report = registry.scan([tmp_path / "work"])

    assert list(found) == ["ok"]
    assert len(report.rejected) == 1
    path, reason = report.rejected[0]
    assert path.endswith("/a/b/c/d/docker-compose.yml")
    assert reason.startswith("too deep: 4 folders below")
    assert "max 3" in reason


def test_scan_reports_rejection_reasons(tmp_path):
    work = tmp_path / "work"
    (work / "nginx").mkdir(parents=True)
    (work / "nginx" / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx\n")
    (work / "nodb").mkdir()
    (work / "nodb" / "docker-compose.yml").write_text("services:\n  web:\n    image: odoo:18\n")
    (work / "broken").mkdir()
    (work / "broken" / "docker-compose.yml").write_text("services: [unclosed\n  - :")
    write_compose(work / "one")
    write_compose(work / "two")  # same acme_web container name -> duplicate slug

    found, report = registry.scan([work])

    assert list(found) == ["acme"]
    reasons = {Path(p).parent.name: r for p, r in report.rejected}
    assert reasons["nginx"] == registry.NO_WEB_REASON
    assert reasons["nodb"] == registry.NO_DB_REASON
    assert reasons["broken"].startswith("YAML parse error:")
    assert reasons["two"].startswith("slug 'acme' already registered from")
    assert reasons["two"].endswith("/one")


# --- heuristic -------------------------------------------------------------------------------


def _write(tmp_path, text):
    d = tmp_path / "proj"
    d.mkdir()
    (d / "docker-compose.yml").write_text(text)
    return d / "docker-compose.yml"


def test_parse_compose_web_env_mentioning_postgres_is_still_web(tmp_path):
    path = _write(
        tmp_path,
        "services:\n"
        "  web:\n    image: odoo:18\n    environment: ['DB_HOST=postgres']\n"
        "  postgres:\n    image: postgres:16\n    depends_on: [web]\n",
    )
    parsed = parse_compose(path)
    assert parsed is not None
    assert parsed[1]["services"] == {"web": "web", "db": "postgres"}


def test_parse_compose_reads_dict_style_environment(tmp_path):
    path = _write(
        tmp_path,
        "services:\n"
        "  web:\n    image: odoo:17\n"
        "  db:\n    image: postgis/postgis\n    environment:\n      POSTGRES_USER: erp\n",
    )
    parsed = parse_compose(path)
    assert parsed is not None
    assert parsed[1]["db_user"] == "erp"


def test_parse_compose_service_named_odoo_with_custom_image(tmp_path):
    path = _write(
        tmp_path,
        "services:\n  odoo:\n    image: mycorp/erp:18\n  db:\n    build:\n      dockerfile: postgres.Dockerfile\n",
    )
    parsed = parse_compose(path)
    assert parsed is not None
    assert parsed[0] == "proj"


# --- roots lifecycle -------------------------------------------------------------------------


def test_default_roots_exclude_cwd():
    assert str(Path.cwd()) not in registry.default_roots()


def test_refresh_registry_normalizes_forgets_and_keeps_cwd_ephemeral(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "default_roots", lambda: [])
    work = tmp_path / "work"
    write_compose(work / "proj")
    cwd = tmp_path / "somewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    cfg, report = registry.refresh_registry(roots=[str(work) + "/", str(work), str(work / ".")])

    assert cfg["roots"] == [str(work.resolve())]
    assert str(cwd.resolve()) in report.ephemeral
    assert str(cwd.resolve()) not in cfg["roots"]
    assert "acme" in cfg["projects"]

    cfg, _ = registry.refresh_registry(forget=[str(work) + "/"])
    assert cfg["roots"] == []


def test_ephemeral_roots_skips_home_and_covered_dirs(tmp_path, monkeypatch):
    proj = tmp_path / "work" / "proj"
    proj.mkdir(parents=True)
    monkeypatch.chdir(proj)
    assert registry.ephemeral_roots([str(tmp_path / "work")]) == []
    assert registry.ephemeral_roots([]) == [str((tmp_path / "work" / "proj").resolve())]
    monkeypatch.chdir(Path.home())
    assert registry.ephemeral_roots([]) == []


def test_scan_overlapping_roots_count_each_compose_file_once(tmp_path):
    outer = tmp_path / "work"
    inner = outer / "team"
    write_compose(inner / "proj")
    (inner / "nginx").mkdir()
    (inner / "nginx" / "docker-compose.yml").write_text("services:\n  app:\n    image: nginx\n")

    found, report = registry.scan([inner], ephemeral=[outer])

    assert list(found) == ["acme"]
    assert report.roots[str(inner.resolve())] == 2
    assert report.roots[str(outer.resolve())] == 0
    assert len(report.rejected) == 1
    assert report.rejected[0][1] == registry.NO_WEB_REASON


def test_scan_nested_root_wins_over_parent_root_depth_limit(tmp_path):
    parent = tmp_path / "work"
    nested = parent / "x" / "y" / "z"
    write_compose(nested / "proj")  # 4 folders below parent, 1 below nested

    found, report = registry.scan([parent, nested])

    assert list(found) == ["acme"]
    assert report.rejected == []
    assert list(report.roots) == [str(parent.resolve()), str(nested.resolve())]
    assert report.roots[str(nested.resolve())] == 1
    assert report.roots[str(parent.resolve())] == 0
