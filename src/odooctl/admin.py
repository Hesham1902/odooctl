from . import compose


def list_candidate_users(project_path, entry, db):
    sql = (
        "SELECT u.id, u.login, p.name FROM res_users u "
        "JOIN res_partner p ON p.id = u.partner_id "
        "WHERE COALESCE(u.share, false) IS NOT TRUE ORDER BY u.id LIMIT 10"
    )
    proc = compose.exec_service(
        project_path, entry["services"]["db"],
        "psql", "-U", entry.get("db_user", "odoo"), "-d", db, "-At", "-F", "|", "-c", sql,
        check=False,
    )
    rows = []
    for line in proc.stdout.decode().splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1], "|".join(parts[2:])))
    return rows


def pick_user(candidates, user_id=None):
    if user_id:
        return user_id
    if len(candidates) >= 2:
        return candidates[1][0]
    if candidates:
        return candidates[0][0]
    return None


def reset_admin(project_path, entry, db, login="admin", password="admin", user_id=None):
    candidates = list_candidate_users(project_path, entry, db)
    target = pick_user(candidates, user_id)
    if not target:
        raise RuntimeError(f"No usable internal user found in '{db}'. Pass --user-id explicitly.")
    old = next((c for c in candidates if c[0] == target), (target, "?", "?"))

    esc_login = login.replace("'", "\\'")
    esc_password = password.replace("\\", "\\\\").replace("'", "\\'")
    script = (
        f"u = env['res.users'].browse({target})\n"
        f"vals = {{'login': '{esc_login}', 'password': '{esc_password}', 'active': True}}\n"
        "if 'totp_secret' in u._fields:\n"
        "    vals['totp_secret'] = False\n"
        "u.write(vals)\n"
        "env.cr.commit()\n"
        "print('ODOOCTL_RESET_OK', u.id, u.login)\n"
    )
    proc = compose.run(
        project_path,
        "run", "--rm", "-T", entry["services"]["web"],
        "odoo", "shell", "-c", "/etc/odoo/odoo.conf", "-d", db, "--no-http",
        input_bytes=script.encode(),
        check=False,
    )
    out = (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace")
    if "ODOOCTL_RESET_OK" not in out or proc.returncode != 0:
        tail = "\n".join(line for line in out.splitlines() if line.strip())[-800:]
        raise RuntimeError(f"Reset failed (rc={proc.returncode}).\n{tail}")
    return {"id": target, "old_login": old[1], "name": old[2]}
