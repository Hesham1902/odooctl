"""Commands that explain local setup failures."""

import click

from .. import doctor as doctor_mod
from .root import main


@main.command(section="Project management")
@click.pass_context
def doctor(ctx):
    """Check Docker, registered projects, SSH tools, and update access."""
    checks = doctor_mod.run_checks()
    for check in checks:
        click.echo(f"[{check.status}] {check.name}: {check.detail}")
        if check.fix:
            click.echo(f"  Fix: {check.fix}")
    failures = sum(check.status == "FAIL" for check in checks)
    warnings = sum(check.status == "WARN" for check in checks)
    click.echo(f"{len(checks) - failures - warnings} passed, {warnings} warnings, {failures} failed.")
    if failures:
        ctx.exit(1)
