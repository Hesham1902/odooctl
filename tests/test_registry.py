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
    (d / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: nginx\n    ports: ['80:80']\n"
    )
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
