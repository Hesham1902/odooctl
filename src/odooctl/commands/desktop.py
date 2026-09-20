import click

from .. import gui
from .root import main


@main.command("gui", section="Project management", aliases=("desktop",))
def gui_command():
    """Open the desktop project manager."""
    try:
        gui.launch()
    except gui.GuiUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
