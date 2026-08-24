import re

# Odoo log lines look like:
#   2026-08-21 10:33:12,455 1234 ERROR acme-prod odoo.http: message
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
ERROR_MARKERS = (" ERROR ", " CRITICAL ")


class ErrorFilter:
    """Streaming filter: feed lines, returns True when the line should be kept.

    A kept block starts at a timestamped ERROR/CRITICAL line and includes all
    following non-timestamped lines (tracebacks, details) until the next
    timestamped line.
    """

    def __init__(self):
        self._in_block = False

    def feed(self, line):
        if TS_RE.match(line):
            self._in_block = any(marker in line for marker in ERROR_MARKERS)
        return self._in_block


def filter_errors(text):
    """Return only the ERROR/CRITICAL blocks (with tracebacks) of a log text."""
    flt = ErrorFilter()
    return "".join(line for line in text.splitlines(keepends=True) if flt.feed(line))
