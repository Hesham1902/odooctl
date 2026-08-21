from conftest import SAMPLE_FAILURE_LOG, SAMPLE_SUCCESS_LOG

from odooctl.admin import pick_user
from odooctl.testing import analyze


def test_pick_user_prefers_second_internal_user():
    candidates = [(1, "__system__", "OdooBot"), (2, "admin", "Boss"), (3, "hr@x", "HR")]
    assert pick_user(candidates) == 2


def test_pick_user_single_candidate():
    assert pick_user([(5, "only@x", "Only")]) == 5


def test_pick_user_explicit_id_wins():
    assert pick_user([(1, "a", "A"), (2, "b", "B")], user_id=7) == 7


def test_pick_user_empty():
    assert pick_user([]) is None


def test_analyze_failure_log():
    result = analyze(SAMPLE_FAILURE_LOG, returncode=0)
    assert result.ok is False
    assert result.failures == ["FAIL: test_compute_net"]
    assert result.ran == 43


def test_analyze_success_log():
    text = SAMPLE_SUCCESS_LOG
    result = analyze(text, returncode=0)
    assert result.ok is True
    assert result.ran == 12
    assert result.failures == []


def test_analyze_error_line_counts_as_failure():
    text = "ERROR: test_controller (odoo.addons.mod.tests.test_http.TestHttp)\nRan 9 tests in 2.0s"
    result = analyze(text, returncode=0)
    assert result.ok is False
    assert result.failures == ["ERROR: test_controller"]
    assert result.ran == 9


def test_analyze_nonzero_rc_fails_even_without_markers():
    result = analyze("2026-08-21 INFO ? odoo: crash", returncode=1)
    assert result.ok is False


def test_analyze_summary_failure_detected():
    text = "odoo.tests.result: 2 failed, 1 error(s) of 50 tests"
    result = analyze(text, returncode=0)
    assert result.ok is False
    assert result.ran == 50
