from odooctl.manifest import list_addons, read_manifest


def test_read_manifest_valid(tmp_path):
    addon = tmp_path / "mod_a"
    addon.mkdir()
    (addon / "__manifest__.py").write_text("{'name': 'Mod A', 'version': '16.0.1.0.0', 'depends': ['base']}")
    mf = read_manifest(addon)
    assert mf["name"] == "Mod A"
    assert mf["depends"] == ["base"]


def test_read_manifest_invalid_syntax(tmp_path):
    addon = tmp_path / "mod_bad"
    addon.mkdir()
    (addon / "__manifest__.py").write_text("{'name': oops")
    assert read_manifest(addon) is None


def test_list_addons_filters_and_sorts(tmp_path):
    root = tmp_path
    for name in ("b_mod", "a_mod", ".hidden", "_private"):
        d = root / name
        d.mkdir()
        (d / "__manifest__.py").write_text("{'name': '%s'}" % name)
    (root / "empty_dir").mkdir()

    addons = list_addons(root)

    assert set(addons) == {"a_mod", "b_mod"}
