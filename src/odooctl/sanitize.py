import json

from . import compose

MARKER = "ODOOCTL_SANITIZE_OK"

CRONS = """
if 'ir.cron' in env:
    env.cr.execute("UPDATE ir_cron SET active = FALSE WHERE active")
    counts['crons_paused'] = env.cr.rowcount
    env.cr.commit()
"""

MAIL = """
if 'mail.mail' in env:
    env.cr.execute("DELETE FROM mail_mail WHERE state = 'outgoing'")
    counts['mails_purged'] = env.cr.rowcount
    env.cr.commit()
if 'ir.mail_server' in env:
    env.cr.execute("UPDATE ir_mail_server SET active = FALSE WHERE active")
    counts['smtp_disabled'] = env.cr.rowcount
    env.cr.commit()
if 'fetchmail.server' in env:
    env.cr.execute("UPDATE fetchmail_server SET active = FALSE WHERE active")
    counts['fetchmail_disabled'] = env.cr.rowcount
    env.cr.commit()
"""

CONTACTS = """
def _scrub(field):
    total = 0
    while True:
        env.cr.execute(
            "UPDATE res_partner SET " + field + " = NULL "
            "WHERE id IN (SELECT id FROM res_partner WHERE " + field + " IS NOT NULL LIMIT 500)")
        done = env.cr.rowcount
        total += done
        env.cr.commit()
        if done == 0:
            break
    return total

counts['emails_scrubbed'] = _scrub('email')
counts['phones_scrubbed'] = _scrub('phone') + _scrub('mobile')
if 'vat' in env['res.partner']._fields:
    counts['vats_scrubbed'] = _scrub('vat')
"""

NAMES = """
env.cr.execute("SELECT count(*) FROM res_partner WHERE name IS NOT NULL AND name <> ''")
counts['names_replaced'] = env.cr.fetchone()[0]
env.cr.execute("UPDATE res_partner SET name = 'Partner #' || id")
env.cr.commit()
"""


def build_script(with_names=False, keep_crons=False, keep_mail=False, scrub_contacts=True):
    parts = ["import json", "counts = {}"]
    if not keep_crons:
        parts.append(CRONS)
    if not keep_mail:
        parts.append(MAIL)
    if scrub_contacts:
        parts.append(CONTACTS)
    if with_names:
        parts.append(NAMES)
    parts.append(f"print('{MARKER}=' + json.dumps(counts))")
    return "\n".join(parts) + "\n"


def parse_output(text):
    for line in text.splitlines():
        if MARKER + "=" in line:
            payload = line.split(MARKER + "=", 1)[1].strip()
            start = payload.find("{")
            end = payload.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(payload[start:end + 1])
                except json.JSONDecodeError:
                    pass
    return None


def sanitize(project_path, entry, db, with_names=False, keep_crons=False, keep_mail=False,
             scrub_contacts=True):
    script = build_script(with_names=with_names, keep_crons=keep_crons,
                          keep_mail=keep_mail, scrub_contacts=scrub_contacts)
    proc = compose.run(
        project_path,
        "run", "--rm", "-T", entry["services"]["web"],
        "odoo", "shell", "-c", "/etc/odoo/odoo.conf", "-d", db, "--no-http",
        input_bytes=script.encode(),
        check=False,
    )
    out = (proc.stdout or b"").decode(errors="replace") + (proc.stderr or b"").decode(errors="replace")
    counts = parse_output(out)
    if counts is None or proc.returncode != 0:
        tail = "\n".join(line for line in out.splitlines() if line.strip())[-800:]
        raise RuntimeError(f"Sanitize failed (rc={proc.returncode}).\n{tail}")
    return counts


LABELS = [
    ("crons_paused", "scheduled actions paused"),
    ("mails_purged", "queued mails deleted"),
    ("smtp_disabled", "outgoing mail servers disabled"),
    ("fetchmail_disabled", "fetchmail servers disabled"),
    ("emails_scrubbed", "partner emails scrubbed"),
    ("phones_scrubbed", "partner phones/mobiles scrubbed"),
    ("vats_scrubbed", "VAT numbers scrubbed"),
    ("names_replaced", "partner names replaced"),
]
