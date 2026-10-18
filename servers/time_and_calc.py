"""time_and_calc MCP server: time/date, timezone and unit conversion, safe math (no eval)."""
from __future__ import annotations

import ast
import datetime as dt
import math
import operator
from zoneinfo import ZoneInfo, available_timezones

from servers._common import make_server

mcp = make_server("time_and_calc")

DEFAULT_TZ = "Africa/Nairobi"

CITY_TZ = {
    "nairobi": "Africa/Nairobi", "kenya": "Africa/Nairobi", "mombasa": "Africa/Nairobi", "kampala": "Africa/Kampala",
    "dar es salaam": "Africa/Dar_es_Salaam", "kigali": "Africa/Kigali", "addis ababa": "Africa/Addis_Ababa",
    "lagos": "Africa/Lagos", "johannesburg": "Africa/Johannesburg", "cairo": "Africa/Cairo", "accra": "Africa/Accra",
    "london": "Europe/London", "uk": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin",
    "dubai": "Asia/Dubai", "mumbai": "Asia/Kolkata", "india": "Asia/Kolkata", "delhi": "Asia/Kolkata",
    "beijing": "Asia/Shanghai", "china": "Asia/Shanghai", "tokyo": "Asia/Tokyo", "singapore": "Asia/Singapore",
    "sydney": "Australia/Sydney", "new york": "America/New_York", "washington": "America/New_York",
    "chicago": "America/Chicago", "los angeles": "America/Los_Angeles", "san francisco": "America/Los_Angeles",
    "toronto": "America/Toronto", "sao paulo": "America/Sao_Paulo", "utc": "UTC", "gmt": "UTC",
}


def _tz(name: str | None) -> ZoneInfo:
    if not name:
        return ZoneInfo(DEFAULT_TZ)
    key = name.strip().lower()
    if key in CITY_TZ:
        return ZoneInfo(CITY_TZ[key])
    for tz in available_timezones():
        if tz.lower() == key or tz.lower().endswith("/" + key.replace(" ", "_")):
            return ZoneInfo(tz)
    raise ValueError(f"Unknown timezone or city: {name}")


@mcp.tool()
def current_time(timezone: str = "") -> str:
    """Current local date and time. Optionally pass a city or IANA timezone, e.g. 'London' or 'Asia/Tokyo'."""
    tz = _tz(timezone or None)
    now = dt.datetime.now(tz)
    return f"{now.strftime('%A %d %B %Y, %H:%M')} ({tz.key}, UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]})"


@mcp.tool()
def convert_time(time: str, from_timezone: str, to_timezone: str) -> str:
    """Convert a time like '14:30' or '2026-10-18 09:00' from one city/timezone to another."""
    src, dst = _tz(from_timezone), _tz(to_timezone)
    today = dt.datetime.now(src).date()
    for fmt in ("%Y-%m-%d %H:%M", "%H:%M", "%I:%M %p", "%I %p", "%I%p"):
        try:
            parsed = dt.datetime.strptime(time.strip().upper() if "%p" in fmt else time.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"Could not parse time: {time}")
    if parsed.year == 1900:
        parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
    out = parsed.replace(tzinfo=src).astimezone(dst)
    return f"{parsed.strftime('%H:%M')} in {src.key} is {out.strftime('%H:%M on %A')} in {dst.key}"


@mcp.tool()
def date_difference(start: str, end: str = "") -> str:
    """Days between two dates (YYYY-MM-DD). If end is empty, uses today."""
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end) if end else dt.datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    d = (b - a).days
    return f"{abs(d)} days {'from' if d >= 0 else 'until'} {a.isoformat()} to {b.isoformat()}"


# ---------------------------------------------------------------- safe math

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log, "log10": math.log10,
          "ln": math.log, "exp": math.exp, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
          "factorial": math.factorial, "min": min, "max": max, "radians": math.radians, "degrees": math.degrees}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


def safe_eval(expr: str) -> float | int:
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", "")
    expr = expr.replace("percent of", "/100*").replace("% of", "/100*")
    tree = ast.parse(expr, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 1000 or abs(left) > 1e6):
                raise ValueError("exponent too large")
            return _BIN[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            args = [ev(a) for a in node.args]
            if node.func.id == "factorial" and args and args[0] > 1000:
                raise ValueError("factorial too large")
            return _FUNCS[node.func.id](*args)
        raise ValueError(f"Unsupported expression element: {ast.dump(node)[:60]}")

    return ev(tree)


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate a math expression safely, e.g. '15% of 2400', '(3+4)*2', 'sqrt(144)', '2^10'."""
    result = safe_eval(expression)
    if isinstance(result, float):
        result = round(result, 10)
        if result.is_integer():
            result = int(result)
    return f"{expression} = {result}"


_ureg = None


def _units():
    global _ureg
    if _ureg is None:
        import pint
        _ureg = pint.UnitRegistry()
    return _ureg


@mcp.tool()
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """Convert between units, e.g. 5 'miles' to 'km', 30 'degC' to 'degF', 2 'cups' to 'ml'."""
    ureg = _units()
    alias = {"celsius": "degC", "c": "degC", "fahrenheit": "degF", "f": "degF", "kelvin": "kelvin",
             "kph": "km/h", "mph": "mile/hour"}
    fu, tu = alias.get(from_unit.lower(), from_unit), alias.get(to_unit.lower(), to_unit)
    q = ureg.Quantity(value, fu).to(tu)
    return f"{value} {from_unit} = {round(q.magnitude, 4)} {to_unit}"


if __name__ == "__main__":
    mcp.run()
