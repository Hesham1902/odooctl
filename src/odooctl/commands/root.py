import time
from collections import defaultdict

import click

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}
SECTION_ORDER = (
    "Project management",
    "Runtime",
    "Development",
    "Database",
    "Storage",
)


class SectionedGroup(click.Group):
    """A flat command group with categorized help and compatibility aliases."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.command_sections = {}
        self.aliases = {}

    def command(self, *args, section="Other", aliases=(), **kwargs):
        decorator = super().command(*args, **kwargs)

        def register(function):
            command = decorator(function)
            self.command_sections[command.name] = section
            for alias in aliases:
                self.aliases[alias] = command.name
            return command

        return register

    def get_command(self, ctx, cmd_name):
        return super().get_command(ctx, self.aliases.get(cmd_name, cmd_name))

    def format_commands(self, ctx, formatter):
        grouped = defaultdict(list)
        aliases_by_command = defaultdict(list)
        for alias, command_name in self.aliases.items():
            aliases_by_command[command_name].append(alias)

        for command_name in self.list_commands(ctx):
            command = self.get_command(ctx, command_name)
            if command is None or command.hidden:
                continue
            display_name = command_name
            aliases = aliases_by_command.get(command_name)
            if aliases:
                display_name += f" ({', '.join(sorted(aliases))})"
            grouped[self.command_sections.get(command_name, "Other")].append(
                (display_name, command.get_short_help_str(limit=formatter.width - 6))
            )

        ordered_sections = list(SECTION_ORDER)
        ordered_sections.extend(sorted(set(grouped) - set(SECTION_ORDER)))
        for section in ordered_sections:
            rows = grouped.get(section)
            if rows:
                with formatter.section(section):
                    formatter.write_dl(rows)

    def invoke(self, ctx):
        started = time.perf_counter()
        try:
            return super().invoke(ctx)
        finally:
            if ctx.params.get("debug"):
                timings = (ctx.obj or {}).get("timings", [])
                if timings:
                    click.echo("\n[debug] timings", err=True)
                    for label, elapsed in timings:
                        click.echo(f"  {label:<24} {elapsed * 1000:>8.1f} ms", err=True)
                click.echo(
                    f"  {'total':<24} {(time.perf_counter() - started) * 1000:>8.1f} ms",
                    err=True,
                )


@click.group(cls=SectionedGroup, context_settings=CONTEXT_SETTINGS)
@click.version_option(package_name="odooctl", prog_name="odooctl")
@click.option("--debug", is_flag=True, help="Show operation timings for troubleshooting.")
@click.pass_context
def main(ctx, debug):
    """One CLI for all your local Odoo docker environments."""
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    ctx.obj.setdefault("timings", [])
