from pathlib import Path

import pytest

from odooctl import compose, provision


def _template_data(version="18", host_prefix="/home/dev"):
    return {
        "services": {
            "web": {
                "container_name": "old_web",
                "build": {"dockerfile": "odoo.Dockerfile"},
                "ports": ["8056:8069", "8072:8072", "8888:8888"],
                "volumes": [
                    "./config:/etc/odoo",
                    f"{host_prefix}/_odoo_addons/odoo-{version}.0/odoo/addons:/mnt/enterprise",
                ],
            },
            "db": {
                "container_name": "old_db",
                "build": {"dockerfile": "postgres.Dockerfile"},
                "ports": ["5456:5432"],
            },
        }
    }


def test_rewrite_compose_renames_and_reallocates():
    def alloc(preferred):
        return preferred + 1000
    data, web, db = provision.rewrite_compose(_template_data(), "acme", "18.0", alloc)

    assert data["name"] == "acme"
    assert data["services"]["web"]["container_name"] == "acme_web"
    assert data["services"]["db"]["container_name"] == "acme_db"
    assert data["services"]["web"]["ports"] == ["9056:8069", "9072:8072", "9888:8888"]
    assert data["services"]["db"]["ports"] == ["6456:5432"]


def test_rewrite_compose_fixes_linux_home_and_version():
    def alloc(p):
        return p + 1
    data, _, _ = provision.rewrite_compose(_template_data(host_prefix="/home/dev"), "c", "17.0", alloc)
    volumes = data["services"]["web"]["volumes"]
    enterprise = [v for v in volumes if "_odoo_addons" in v][0]
    assert enterprise.startswith(str(Path.home()))
    assert "odoo-17.0" in enterprise
    assert "./config:/etc/odoo" in volumes


def test_rewrite_compose_keeps_mac_style_path_when_already_local(tmp_path):
    local = tmp_path / "_odoo_addons" / "odoo-18.0" / "odoo" / "addons"
    data = _template_data()
    data["services"]["web"]["volumes"] = [f"{local}:/mnt/enterprise"]
    def alloc(p):
        return p + 1
    new_data, _, _ = provision.rewrite_compose(data, "c", None, alloc)
    vol = new_data["services"]["web"]["volumes"][0]
    assert str(local) in vol


def test_ports_of_mapping():
    def alloc(p):
        return p + 1000
    data, web, db = provision.rewrite_compose(_template_data(), "x", None, alloc)
    ports = provision.ports_of(data, web, db)
    assert ports == {"http": 9056, "longpolling": 9072, "debugpy": 9888, "pg_postgres": 6456}


def test_allocator_skips_taken_ports():
    alloc = provision._allocator({8056})
    picked = alloc(8056)
    assert picked != 8056
    second = alloc(8057)
    assert second != picked


def test_pick_template_requires_version_or_template():
    with pytest.raises(RuntimeError):
        provision.pick_template({"a": {}}, None, None)


def test_pick_template_unknown_template():
    with pytest.raises(RuntimeError):
        provision.pick_template({"a": {}}, None, "nope")


def test_pick_template_no_candidates_lists_known():
    projects = {"a": {"path": "/a"}, "b": {"path": "/b"}}
    with pytest.raises(RuntimeError) as err:
        provision.pick_template(projects, "16.0", None)
    assert "'a': None" in str(err.value) or "a" in str(err.value)


def test_pick_template_prefers_project_with_built_image(tmp_path, monkeypatch):
    from conftest import write_compose

    from odooctl.registry import parse_compose

    def entry_for(name):
        d = write_compose(tmp_path / name, version="18", host_prefix="/Users/dev")
        return parse_compose(d / "docker-compose.yml")[1]

    projects = {"aaa": entry_for("aaa"), "zzz": entry_for("zzz")}

    monkeypatch.setattr(compose, "local_image", lambda name: None)
    slug, _ = provision.pick_template(projects, "18.0", None)
    assert slug == "aaa"

    def fake_image(name):
        return name if "zzz" in name else None

    monkeypatch.setattr(compose, "local_image", fake_image)
    slug, _ = provision.pick_template(projects, "18.0", None)
    assert slug == "zzz"


def test_rewrite_compose_strips_explicit_image():
    data = _template_data()
    data["services"]["web"]["image"] = "custom-odoo:16"

    def alloc(p):
        return p + 1

    new_data, _, _ = provision.rewrite_compose(data, "c", None, alloc)
    assert "image" not in new_data["services"]["web"]
    assert "build" in new_data["services"]["web"]


def test_find_built_image_prefers_explicit_then_convention(tmp_path, monkeypatch):
    from odooctl import compose

    d = tmp_path / "proj"
    d.mkdir()
    (d / "docker-compose.yml").write_text(
        "name: proj\nservices:\n  web:\n    image: team/odoo-custom:18\n"
        "  db:\n    image: postgres:16\n"
    )
    entry = {
        "compose_file": str(d / "docker-compose.yml"),
        "path": str(d),
        "services": {"web": "web", "db": "db"},
        "images": {"web": "team/odoo-custom:18", "db": "postgres:16"},
    }

    monkeypatch.setattr(
        compose, "local_image",
        lambda name: name if name == "team/odoo-custom:18" else None,
    )
    assert compose.find_built_image(entry, "web") == "team/odoo-custom:18"


def test_find_built_image_falls_back_through_candidates(tmp_path, monkeypatch):
    from odooctl import compose

    d = tmp_path / "some-folder"
    d.mkdir()
    (d / "docker-compose.yml").write_text(
        "name: team-stack\nservices:\n  web:\n    build: .\n"
    )
    entry = {
        "compose_file": str(d / "docker-compose.yml"),
        "path": str(d),
        "services": {"web": "web"},
        "images": {"web": None},
    }
    seen = []

    def fake_local(name):
        seen.append(name)
        return None if name == "some-folder-web" else name

    monkeypatch.setattr(compose, "local_image", fake_local)
    found = compose.find_built_image(entry, "web")
    assert seen == ["some-folder-web", "team-stack-web"]
    assert found == "team-stack-web"


def test_slugify():
    assert provision.slugify("New Client 18") == "new-client-18"
    assert provision.slugify("My_Project!") == "my-project"
    assert provision.slugify("///") is None
