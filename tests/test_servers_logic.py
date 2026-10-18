"""Unit tests for pure logic inside the built-in servers (no MCP transport needed)."""
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from servers.notes_reminders import parse_when
from servers.time_and_calc import calculate, safe_eval


def test_safe_eval_basic():
    assert safe_eval("2+3*4") == 14
    assert safe_eval("2^10") == 1024
    assert safe_eval("sqrt(144)") == 12
    assert safe_eval("15% of 2400") == 360
    assert calculate("1,500 * 3") == "1,500 * 3 = 4500"


@pytest.mark.parametrize("bad", ["__import__('os')", "open('x')", "(1).__class__", "a", "9**9**9", "factorial(5000)", "lambda: 1"])
def test_safe_eval_rejects_code(bad):
    with pytest.raises((ValueError, SyntaxError)):
        safe_eval(bad)


def test_parse_when():
    now = dt.datetime(2026, 10, 18, 14, 0, tzinfo=ZoneInfo("Africa/Nairobi"))
    assert parse_when("in 10 minutes", now) == now + dt.timedelta(minutes=10)
    assert parse_when("in 2 hours", now) == now + dt.timedelta(hours=2)
    assert parse_when("17:30", now).hour == 17
    assert parse_when("5pm", now).hour == 17
    assert parse_when("9am", now).day == 19  # already past today -> tomorrow
    assert parse_when("tomorrow 8am", now).day == 19
    with pytest.raises(ValueError):
        parse_when("whenever", now)
