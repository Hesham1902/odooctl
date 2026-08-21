import dataclasses
import datetime
import re
from pathlib import Path

from . import compose

FAIL_LINE_RE = re.compile(r"\b(FAIL|ERROR): (\w[\w\.\$]*)")
SUMMARY_RE = re.compile(r"(\d+) failed(?:,\s*(\d+) error\(s\))?\s+of\s+(\d+) tests")
RAN_RE = re.compile(r"of\s+(\d+)\s+tests|(?<![\w])Ran\s+(\d+)\s+tests")


@dataclasses.dataclass
class TestResult:
    ok: bool
    ran: int | None
    failures: list[str]
    log_path: Path | None
    raw_tail: str


def odoo_command(module, db, test_tags=None):
    tags = test_tags or f"/{module}"
    return [
        "odoo",
        "-c", "/etc/odoo/odoo.conf",
        "-d", db,
        "-i", module,
        "--test-enable",
        "--test-tags", tags,
        "--stop-after-init",
    ]


def drop_db_if_exists(project_path, entry, db):
    user = entry.get("db_user", "odoo")
    compose.exec_service(
        project_path, "db", "psql", "-U", user, "-d", "postgres",
        "-c", f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db}' AND pid <> pg_backend_pid()",
    )
    compose.exec_service(project_path, "db", "psql", "-U", user, "-d", "postgres",
                         "-c", f'DROP DATABASE IF EXISTS "{db}"')


def analyze(text, returncode):
    failures = [f"{level}: {name}" for level, name in FAIL_LINE_RE.findall(text)]
    summary = SUMMARY_RE.search(text)
    ran_match = RAN_RE.search(text)
    ran = None
    if summary:
        ran = int(summary.group(3))
    elif ran_match:
        groups = ran_match.groups()
        ran = int(groups[0] or groups[1])

    summary_failed = summary is not None and (
        int(summary.group(1)) > 0 or int(summary.group(2) or 0) > 0
    )
    ok = returncode == 0 and not failures and not summary_failed

    tail_lines = [line for line in text.splitlines() if line.strip()][-15:]
    return TestResult(ok=ok, ran=ran, failures=failures, log_path=None,
                      raw_tail="\n".join(tail_lines))


def run_tests(project_path, entry, module, db, test_tags=None, keep_db=False, timeout=None):
    web = entry["services"]["web"]
    result = compose.run(
        project_path,
        "run", "--rm", web,
        *odoo_command(module, db, test_tags),
        capture=True,
        check=False,
        timeout=timeout,
    )
    text = (result.stdout or b"").decode(errors="replace") + "\n" + (result.stderr or b"").decode(errors="replace")

    analysis = analyze(text, result.returncode)

    log_path = None
    logs_dir = Path(entry["path"]) / "backups" / "test_logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = logs_dir / f"{module}_{stamp}.log"
        log_path.write_text(text, encoding="utf-8")
    except OSError:
        pass

    if not keep_db:
        try:
            drop_db_if_exists(project_path, entry, db)
        except compose.DockerError:
            pass

    analysis.log_path = log_path
    return analysis
