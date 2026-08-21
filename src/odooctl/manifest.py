import ast
from pathlib import Path


def read_manifest(addon_dir: Path):
    path = addon_dir / "__manifest__.py"
    if not path.is_file():
        return None
    try:
        return ast.literal_eval(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_addons(custom_addons_dir):
    root = Path(custom_addons_dir)
    if not root.is_dir():
        return {}
    addons = {}
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith((".", "_")):
            manifest = read_manifest(child)
            if manifest is not None:
                addons[child.name] = manifest
    return addons
