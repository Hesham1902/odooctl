from odooctl.logparse import ErrorFilter, filter_errors

LOG = """2026-08-21 10:00:00,001 100 INFO acme odoo.modules: loading 42 modules
2026-08-21 10:00:05,002 100 WARNING acme odoo.addons.sale: deprecation ahead
2026-08-21 10:00:06,003 100 INFO acme werkzeug: GET /web/login 200
2026-08-21 10:00:07,004 100 ERROR acme odoo.http: Exception during request
Traceback (most recent call last):
  File "/opt/odoo/x.py", line 10, in go
    boom()
ValueError: boom
2026-08-21 10:00:08,005 100 INFO acme werkzeug: GET /web/login 200
2026-08-21 10:00:09,006 100 CRITICAL acme odoo.service.server: worker timeout
"""


def test_filter_keeps_error_blocks_with_traceback():
    out = filter_errors(LOG)
    assert "Exception during request" in out
    assert "Traceback (most recent call last):" in out
    assert "ValueError: boom" in out
    assert "worker timeout" in out


def test_filter_drops_info_and_warning():
    out = filter_errors(LOG)
    assert "loading 42 modules" not in out
    assert "deprecation ahead" not in out
    assert "GET /web/login" not in out


def test_filter_block_ends_at_next_timestamp():
    out = filter_errors(LOG)
    lines = [ln for ln in out.splitlines()]
    assert lines[-1].startswith("2026-08-21 10:00:09")


def test_streaming_filter_matches_batch():
    flt = ErrorFilter()
    streamed = [ln for ln in LOG.splitlines() if flt.feed(ln)]
    batched = filter_errors(LOG).splitlines()
    assert streamed == batched


def test_no_errors_returns_empty():
    assert filter_errors("2026-08-21 10:00:00,001 1 INFO db hi\n") == ""


def test_multiline_without_leading_timestamp_stays_in_previous_block():
    text = (
        "2026-08-21 10:00:00,001 1 INFO db ok\n"
        "2026-08-21 10:00:01,002 1 ERROR db fail:\n"
        "  detail line one\n"
        "  detail line two\n"
    )
    out = filter_errors(text)
    assert "detail line one" in out
    assert "detail line two" in out
