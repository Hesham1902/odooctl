"""PyInstaller entry point for the desktop application."""

from odooctl.gui import launch

if __name__ == "__main__":
    raise SystemExit(launch())
