import json

from . import compose

MARKER = "ODOOCTL_ICONS_OK"

# Odoo stores menu icons as ir_attachment records whose bytes live in the
# filestore. A restore without a filestore leaves those files missing, so every
# app menu renders a placeholder. The PNGs still exist inside the addon source
# folders, so we re-import them.

SCRIPT = """
import base64
import glob
import json
import os

try:
    from odoo.modules.module import get_module_path
except ImportError:
    def get_module_path(module, display_warning=False):
        return None

checked = 0
fixed = 0
missing_sources = []

# full_list: ir.ui.menu.search otherwise drops menus hidden by groups
# (e.g. Link Tracker needs debug mode), leaving their icons unrepaired
menus = env['ir.ui.menu'].with_context(active_test=False, **{'ir.ui.menu.full_list': True}).search([
    ('web_icon', '!=', False), ('web_icon', '!=', ''),
])

roots = ['/mnt/extra-addons', '/usr/lib/python3/dist-packages']
roots += sorted(glob.glob('/usr/lib/python3*/site-packages'))

# Odoo 16+ renders menu icons from the stored binary field web_icon_data
# (backed by an ir.attachment with res_field='web_icon_data'). Older versions
# read a name-matched attachment instead.
has_icon_field = 'web_icon_data' in env['ir.ui.menu']._fields

# clean up attachments created by older versions of this script - they are
# not what the UI renders from
legacy = env['ir.attachment'].search([
    ('res_model', '=', 'ir.ui.menu'), ('res_field', '=', False),
    ('name', 'like', ','),
])
if has_icon_field and legacy:
    legacy.unlink()

for menu in menus:
    module, sep, path = (menu.web_icon or '').partition(',')
    # "fa fa-cubes,#color,#background" is a Font Awesome icon, not a file
    if not sep or not path or module.startswith('fa '):
        continue
    checked += 1

    attachment = env['ir.attachment']
    intact = False
    try:
        if has_icon_field:
            intact = bool(menu.web_icon_data)
        else:
            for att in env['ir.attachment'].search([
                ('res_model', '=', 'ir.ui.menu'), ('res_id', '=', menu.id),
            ]):
                if att.name == menu.web_icon or (att.name or '').endswith(path):
                    attachment = att
                    break
            if attachment and attachment.db_datas:
                intact = True
            elif attachment and attachment.store_fname:
                intact = os.path.isfile(attachment._full_path(attachment.store_fname))
    except OSError:
        intact = False

    if intact:
        continue

    data = None
    # the configured addons_path first (covers enterprise and any extra mount),
    # then the fixed roots as a fallback
    module_dir = get_module_path(module, display_warning=False)
    candidates = [os.path.join(module_dir, path)] if module_dir else []
    candidates += [os.path.join(root, prefix, module, path)
                   for root in roots for prefix in ('', 'odoo/addons')]
    for candidate in candidates:
        if os.path.isfile(candidate):
            with open(candidate, 'rb') as fh:
                data = fh.read()
            break
    if data is None:
        missing_sources.append(menu.web_icon)
        continue

    if has_icon_field:
        menu.web_icon_data = base64.b64encode(data)
    elif attachment:
        try:
            attachment.write({'raw': data, 'store_fname': False})
            fixed += 1
            continue
        except OSError:
            attachment.unlink()
    if not has_icon_field and not attachment:
        env['ir.attachment'].create({
            'name': menu.web_icon,
            'res_model': 'ir.ui.menu',
            'res_id': menu.id,
            'raw': data,
        })
    fixed += 1

env.cr.commit()
print('{MARKER}=' + json.dumps({'checked': checked, 'fixed': fixed,
                                'unrepairable': len(missing_sources)}))
""".replace("{MARKER}", MARKER)


def parse_output(text):
    for line in text.splitlines():
        if MARKER + "=" in line:
            payload = line.split(MARKER + "=", 1)[1].strip()
            start, end = payload.find("{"), payload.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(payload[start : end + 1])
                except json.JSONDecodeError:
                    pass
    return None


def fix_icons(project_path, entry, db):
    proc = compose.run(
        project_path,
        "run",
        "--rm",
        "-T",
        entry["services"]["web"],
        "odoo",
        "shell",
        "-c",
        "/etc/odoo/odoo.conf",
        "-d",
        db,
        "--no-http",
        input_bytes=SCRIPT.encode(),
        check=False,
    )
    out = (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace")
    counts = parse_output(out)
    if counts is None or proc.returncode != 0:
        tail = "\n".join(line for line in out.splitlines() if line.strip())[-800:]
        raise RuntimeError(f"Icon repair failed (rc={proc.returncode}).\n{tail}")
    return counts
