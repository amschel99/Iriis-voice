"""money MCP server: personal and business money tracker in KES (Postgres on Railway, SQLite locally).

Transactions and monthly budgets live in the shared database, so they follow the user to the cloud.
Pasted M-Pesa confirmation SMSes are parsed into transactions (duplicates skipped by M-Pesa code).
The pure helpers (parse_period, parse_when_money, parse_mpesa_messages, categorise, fmt_kes) need no
MCP transport and are unit-tested directly.
"""
from __future__ import annotations

import calendar
import csv
import datetime as dt
import io
import re
import time
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from servers._common import make_server
from iriis.storage import get_db

mcp = make_server("money")
TZ = ZoneInfo("Africa/Nairobi")
KINDS = ("expense", "income", "transfer")
_KIND_ALIASES = {
    "expense": "expense", "expenses": "expense", "spent": "expense", "spend": "expense", "out": "expense",
    "paid": "expense", "payment": "expense", "cost": "expense", "debit": "expense",
    "income": "income", "in": "income", "received": "income", "receive": "income", "sale": "income",
    "sales": "income", "earned": "income", "credit": "income",
    "transfer": "transfer", "transfers": "transfer", "withdrawal": "transfer", "withdraw": "transfer",
    "deposit": "transfer", "move": "transfer",
}
_COLS = "id, ts, amount, currency, kind, category, party, note, account, ref, business"


# ---------------------------------------------------------------------------------------- formatting

def fmt_kes(amount: float, currency: str = "KES") -> str:
    """1250 -> 'KES 1,250'; 1250.5 -> 'KES 1,250.50'."""
    a = round(float(amount), 2)
    s = f"{a:,.0f}" if a == int(a) else f"{a:,.2f}"
    return f"{currency or 'KES'} {s}"


def _day(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%a %d %b")


def _stamp(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%a %d %b %H:%M")


def normalise_kind(kind: str) -> str:
    k = _KIND_ALIASES.get((kind or "expense").strip().lower())
    if not k:
        raise ValueError(f"Kind must be expense, income or transfer, not '{kind}'.")
    return k


# ---------------------------------------------------------------------------------------- categories

# Ordered: the first category with a matching keyword wins ("school fees" before "fees").
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("school", ("school fees", "school", "tuition", "university", "college", "exam fee")),
    ("rent", ("rent", "landlord", "caretaker", "house rent", "office rent")),
    ("utilities", ("kplc", "kenya power", "token", "tokens", "electricity", "nairobi water", "water",
                   "zuku", "home fibre", "fibre", "wifi", "internet", "faiba", "gas", "garbage", "dstv", "gotv")),
    ("airtime", ("airtime", "bundle", "bundles", "data", "safaricom airtime", "credit top up", "tunukiwa")),
    ("transport", ("matatu", "uber", "bolt", "little cab", "taxi", "boda", "fare", "fuel", "petrol",
                   "diesel", "shell", "rubis", "totalenergies", "total energies", "parking", "sgr",
                   "bus", "train", "toll", "car wash", "mechanic")),
    ("groceries", ("naivas", "carrefour", "quickmart", "chandarana", "magunas", "cleanshelf", "eastmatt",
                   "groceries", "grocery", "supermarket", "mama mboga", "market")),
    ("food", ("food", "lunch", "dinner", "breakfast", "restaurant", "cafe", "coffee", "java", "kfc",
              "pizza", "chicken", "nyama choma", "chapati", "artcaffe", "hotel", "glovo", "uber eats",
              "snack", "drinks", "chai")),
    ("health", ("pharmacy", "chemist", "hospital", "clinic", "doctor", "medicine", "nhif", "sha", "dentist")),
    ("entertainment", ("netflix", "showmax", "spotify", "movie", "cinema", "youtube premium", "club", "betting")),
    ("supplies", ("supplies", "supply", "stock", "materials", "hardware", "stationery", "packaging",
                  "inventory", "raw material", "cement", "timber", "printing")),
    ("salary", ("salary", "wages", "wage", "payroll", "stipend", "allowance")),
    ("sales", ("sale", "sales", "customer", "client", "invoice", "order")),
    ("savings", ("m-shwari", "mshwari", "sacco", "savings", "lock savings", "ziidi", "mmf")),
    ("loans", ("fuliza", "loan", "kcb m-pesa", "hustler fund", "tala", "branch")),
    ("fees", ("fee", "fees", "charge", "charges", "transaction cost", "commission")),
]


def categorise(party: str = "", note: str = "", kind: str = "expense") -> str:
    """Guess a category from who was paid and the note. Falls back to 'other' (or 'income')."""
    text = f"{party} {note}".lower()
    for cat, words in CATEGORY_KEYWORDS:
        for w in words:
            if re.search(rf"(?<![a-z0-9]){re.escape(w)}s?(?![a-z0-9])", text):
                return cat
    return "income" if kind == "income" else "transfer" if kind == "transfer" else "other"


# ---------------------------------------------------------------------------------------- dates

def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(TZ)


def _parse_clock(s: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap])?\.?m?\.?", s.strip().lower())
    if not m or (not m.group(2) and not m.group(3)):
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        h = h % 12 + (12 if m.group(3) == "p" else 0)
    if h > 23 or mi > 59:
        return None
    return h, mi


def _parse_date(s: str) -> dt.date | None:
    s = s.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", s)   # Kenyan order: day/month/year
    if m:
        y = int(m.group(3))
        return dt.date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    return None


def parse_when_money(when: str, now: dt.datetime | None = None) -> dt.datetime:
    """When a transaction happened: '' / 'now', 'today', 'yesterday', 'yesterday 3pm', '3 days ago',
    '2026-10-19', '2026-10-19 14:30', '19/10/26 2:15 PM'. Nairobi time. A bare date means midday."""
    now = _now(now)
    s = (when or "").strip().lower()
    if s in ("", "now", "just now"):
        return now
    m = re.fullmatch(r"(\d+)\s+days?\s+ago", s)
    if m:
        return now - dt.timedelta(days=int(m.group(1)))
    day: dt.date | None = None
    rest = s
    for word, offset in (("day before yesterday", 2), ("yesterday", 1), ("today", 0)):
        if s.startswith(word):
            day, rest = now.date() - dt.timedelta(days=offset), s[len(word):].strip().removeprefix("at").strip()
            break
    if day is None:
        parts = s.replace("t", " ", 1).split(None, 1) if re.match(r"\d{4}-\d{2}-\d{2}t", s) else s.split(None, 1)
        day = _parse_date(parts[0]) if parts else None
        rest = parts[1].strip().removeprefix("at").strip() if len(parts) > 1 else ""
        if day is None:
            clock = _parse_clock(s)
            if clock:
                return now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            raise ValueError(f"Could not understand the date '{when}'. Try 'today', 'yesterday' or '2026-10-19 14:30'.")
        if not rest:
            return dt.datetime(day.year, day.month, day.day, 12, 0, tzinfo=TZ)
    if not rest:
        if day == now.date():
            return now
        return dt.datetime(day.year, day.month, day.day, now.hour, now.minute, tzinfo=TZ)
    clock = _parse_clock(rest)
    if not clock:
        raise ValueError(f"Could not understand the time in '{when}'.")
    return dt.datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=TZ)


@dataclass
class Period:
    start: dt.datetime
    end: dt.datetime            # exclusive
    label: str
    prev_start: dt.datetime
    prev_end: dt.datetime
    prev_label: str

    @property
    def bounds(self) -> tuple[float, float]:
        return self.start.timestamp(), self.end.timestamp()

    @property
    def prev_bounds(self) -> tuple[float, float]:
        return self.prev_start.timestamp(), self.prev_end.timestamp()


def _midnight(d: dt.date) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, tzinfo=TZ)


def _month_start(y: int, m: int) -> dt.datetime:
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return dt.datetime(y, m, 1, tzinfo=TZ)


def parse_period(period: str = "this month", now: dt.datetime | None = None) -> Period:
    """'today', 'yesterday', 'this week', 'last week', 'this month', 'last month', 'this year', 'last year',
    'last 7 days', 'last 30 days', 'all', '2026-10', '2026', '2026-10-19', '2026-10-01..2026-10-15'.
    The previous period has equal length; a period still running is compared to the same point before."""
    now = _now(now)
    s = (period or "this month").strip().lower()
    today = now.date()
    monthly = False   # month-aligned: compare with the previous calendar month(s)
    yearly = False
    if s in ("today",):
        start, end, label = _midnight(today), _midnight(today + dt.timedelta(days=1)), "today"
    elif s == "yesterday":
        start, end, label = _midnight(today - dt.timedelta(days=1)), _midnight(today), "yesterday"
    elif s in ("this week", "week"):
        start = _midnight(today - dt.timedelta(days=today.weekday()))
        end, label = start + dt.timedelta(days=7), "this week"
    elif s == "last week":
        end = _midnight(today - dt.timedelta(days=today.weekday()))
        start, label = end - dt.timedelta(days=7), "last week"
    elif s in ("this month", "month", ""):
        start, end, label, monthly = _month_start(today.year, today.month), _month_start(today.year, today.month + 1), "this month", True
    elif s == "last month":
        start, end, label, monthly = _month_start(today.year, today.month - 1), _month_start(today.year, today.month), "last month", True
    elif s in ("this year", "year"):
        start, end, label, yearly = _midnight(dt.date(today.year, 1, 1)), _midnight(dt.date(today.year + 1, 1, 1)), "this year", True
    elif s == "last year":
        start, end, label, yearly = _midnight(dt.date(today.year - 1, 1, 1)), _midnight(dt.date(today.year, 1, 1)), "last year", True
    elif s in ("all", "all time", "ever", "everything"):
        start, end = dt.datetime(1970, 1, 2, tzinfo=TZ), _midnight(today + dt.timedelta(days=1))
        return Period(start, end, "all time", start, start, "nothing")
    elif m := re.fullmatch(r"(?:last|past)\s+(\d+)\s+(days?|weeks?|months?)", s):
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith("month"):
            start, end, monthly = _month_start(today.year, today.month - n + 1), _month_start(today.year, today.month + 1), True
        else:
            days = n * (7 if unit.startswith("week") else 1)
            start, end = _midnight(today - dt.timedelta(days=days - 1)), _midnight(today + dt.timedelta(days=1))
        label = f"the last {n} {unit}"
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})", s):
        y, mo = int(m.group(1)), int(m.group(2))
        start, end, monthly = _month_start(y, mo), _month_start(y, mo + 1), True
        label = start.strftime("%B %Y")
    elif m := re.fullmatch(r"(\d{4})", s):
        y = int(m.group(1))
        start, end, yearly, label = _midnight(dt.date(y, 1, 1)), _midnight(dt.date(y + 1, 1, 1)), True, str(y)
    elif m := re.fullmatch(r"(\S+)\s*(?:\.\.|to|–|—)\s*(\S+)", s):
        a, b = _parse_date(m.group(1)), _parse_date(m.group(2))
        if not a or not b:
            raise ValueError(f"Could not understand the dates in '{period}'. Use 2026-10-01..2026-10-15.")
        if b < a:
            a, b = b, a
        start, end = _midnight(a), _midnight(b + dt.timedelta(days=1))
        label = f"{a.strftime('%d %b')} to {b.strftime('%d %b %Y')}"
    elif d := _parse_date(s):
        start, end, label = _midnight(d), _midnight(d + dt.timedelta(days=1)), d.strftime("%A %d %B %Y")
    else:
        raise ValueError(f"Could not understand the period '{period}'. Try 'this month', 'last week' or '2026-10'.")

    running = start <= now < end
    elapsed = (now - start) if running else (end - start)
    if monthly or yearly:
        months = 12 if yearly else (end.year - start.year) * 12 + end.month - start.month
        prev_start = _month_start(start.year, start.month - months)
        prev_full_end = start
    else:
        prev_start = start - (end - start)
        prev_full_end = start
    prev_end = min(prev_start + elapsed, prev_full_end) if running else prev_full_end
    if running and label in ("this month", "this week", "this year", "today"):
        prev_label = {"this month": "the same point last month", "this week": "the same point last week",
                      "this year": "the same point last year", "today": "yesterday by this time"}[label]
    elif yearly:
        prev_label = str(prev_start.year)
    elif monthly and months == 1:
        prev_label = prev_start.strftime("%B %Y")
    else:
        prev_label = "the period before"
    return Period(start, end, label, prev_start, prev_end, prev_label)


# ---------------------------------------------------------------------------------------- M-Pesa

@dataclass
class MpesaTxn:
    ref: str
    amount: float
    kind: str
    party: str
    category: str
    ts: float | None
    note: str = ""
    cost: float = 0.0
    raw: str = field(default="", repr=False)


_AMT = r"(?:ksh|kes)\s*\.?\s*([\d,]+(?:\.\d{1,2})?)"
_CODE_START = re.compile(r"(?<![A-Za-z0-9])([A-Z0-9]{8,12})\s+confirmed", re.I)
_PHONE = re.compile(r"\s*(?:\+?254|0)[17]\d{8}\s*$")
_WHEN = re.compile(r"on\s+(\d{1,2}/\d{1,2}/\d{2,4})(?:\s+at\s+(\d{1,2}:\d{2}\s*[ap]\.?m\.?))?", re.I)


def _money(s: str) -> float:
    return float(s.replace(",", ""))


def _clean_party(p: str) -> tuple[str, str]:
    """('JOHN DOE 0712345678.') -> ('JOHN DOE', '0712345678')."""
    p = re.sub(r"\s+", " ", p).strip(" .,-")
    phone = ""
    m = _PHONE.search(p)
    if m:
        phone, p = m.group(0).strip(), p[:m.start()].strip(" .,-")
    return p, phone


def split_mpesa_messages(text: str) -> list[str]:
    """Split a paste of one or many M-Pesa SMSes into single messages (each starts at its code)."""
    text = (text or "").strip()
    starts = [m.start() for m in _CODE_START.finditer(text)]
    if not starts:
        return [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]
    chunks = []
    if text[:starts[0]].strip():
        chunks.append(text[:starts[0]].strip())
    for a, b in zip(starts, starts[1:] + [len(text)]):
        # a blank line also ends a message, so stray text pasted after one is reported on its own
        chunks += [c.strip() for c in re.split(r"\n\s*\n", text[a:b]) if c.strip()]
    return chunks


def parse_mpesa_message(msg: str) -> MpesaTxn | None:
    """Parse one Safaricom M-Pesa confirmation. Returns None when it is not a recognisable transaction."""
    flat = re.sub(r"\s+", " ", msg).strip()
    low = flat.lower()
    if "failed" in low or "reversal" in low or "reversed" in low or "insufficient" in low:
        return None
    code = _CODE_START.search(flat)
    ref = code.group(1).upper() if code and code.start() < 5 else ""

    ts = None
    w = _WHEN.search(flat)
    if w:
        try:
            when = w.group(1) + (" " + w.group(2).replace(".", "") if w.group(2) else "")
            ts = parse_when_money(when).timestamp()
        except ValueError:
            ts = None
    cost = 0.0
    c = re.search(r"transaction cost,?\s*" + _AMT, flat, re.I)
    if c:
        cost = _money(c.group(1))
    end = r"(?=\s+on\s+\d{1,2}/|\.\s|\s+new\s+m-?pesa|\s*$)"

    kind, party, note, category, amount = "", "", "", "", None
    if m := re.search(r"you have received\s+" + _AMT + r"\s+from\s+(.+?)" + end, flat, re.I):
        amount, kind = _money(m.group(1)), "income"
        party, phone = _clean_party(m.group(2))
        note = f"received via M-Pesa{' from ' + phone if phone else ''}"
        category = categorise(party, "", "income")
    elif m := re.search(r"withdraw\s+" + _AMT + r"\s+from\s+(.+?)" + end, flat, re.I):
        amount, kind, category = _money(m.group(1)), "transfer", "cash withdrawal"
        party = re.sub(r"^\d+\s*-\s*", "", _clean_party(m.group(2))[0])
        note = "M-Pesa cash withdrawal"
    elif m := re.search(r"give\s+" + _AMT + r"\s+cash\s+to\s+(.+?)" + end, flat, re.I):
        amount, kind, category = _money(m.group(1)), "transfer", "deposit"
        party = re.sub(r"^\d+\s*-\s*", "", _clean_party(m.group(2))[0])
        note = "M-Pesa cash deposit"
    elif m := re.search(r"you bought\s+" + _AMT + r"\s+of\s+(airtime|bundles?|data)(?:\s+for\s+(\S+?))?" + end, flat, re.I):
        amount, kind, category = _money(m.group(1)), "expense", "airtime"
        party = "Safaricom " + m.group(2).lower()
        note = f"for {m.group(3)}" if m.group(3) else ""
    elif m := re.search(_AMT + r"\s+sent to\s+(.+?)\s+for account\s+(.+?)" + end, flat, re.I):
        amount, kind = _money(m.group(1)), "expense"
        party = _clean_party(m.group(2))[0]
        note = f"Pay Bill, account {m.group(3).strip(' .')}"
        category = categorise(party, note)
    elif m := re.search(_AMT + r"\s+sent to\s+(.+?)" + end, flat, re.I):
        amount, kind = _money(m.group(1)), "expense"
        party, phone = _clean_party(m.group(2))
        note = f"sent via M-Pesa{' to ' + phone if phone else ''}"
        category = categorise(party, "")
    elif m := re.search(_AMT + r"\s+paid to\s+(.+?)" + end, flat, re.I):
        amount, kind = _money(m.group(1)), "expense"
        party = _clean_party(m.group(2))[0]
        note = "Buy Goods via M-Pesa"
        category = categorise(party, "")
    if amount is None or amount <= 0:
        return None
    return MpesaTxn(ref=ref, amount=amount, kind=kind, party=party, category=category or "other",
                    ts=ts, note=note, cost=cost, raw=flat)


def parse_mpesa_messages(text: str) -> tuple[list[MpesaTxn], list[str]]:
    """All transactions found in a paste, plus the messages that could not be understood."""
    found, skipped = [], []
    for chunk in split_mpesa_messages(text):
        t = parse_mpesa_message(chunk)
        if t:
            found.append(t)
        else:
            snippet = re.sub(r"\s+", " ", chunk)
            skipped.append(snippet[:70] + ("..." if len(snippet) > 70 else ""))
    return found, skipped


# ---------------------------------------------------------------------------------------- db helpers

def _ref_exists(db, ref: str) -> bool:
    return bool(ref) and bool(db.execute("SELECT 1 FROM transactions WHERE ref = ?", (ref,)))


def _insert(db, *, ts: float, amount: float, kind: str, category: str, party: str, note: str,
            account: str, ref: str, business: str) -> int:
    return db.insert(
        "INSERT INTO transactions (ts, amount, currency, kind, category, party, note, account, ref, business, created) "
        "VALUES (?, ?, 'KES', ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, round(float(amount), 2), kind, category, party, note, account or "mpesa", ref, business or "personal",
         time.time()))


def _query(db, start: float, end: float, *, business: str = "", category: str = "", kind: str = "",
           search: str = "", order: str = "ts DESC, id DESC", limit: int | None = None) -> list[tuple]:
    sql = f"SELECT {_COLS} FROM transactions WHERE ts >= ? AND ts < ?"
    params: list = [start, end]
    if business:
        sql += " AND LOWER(business) = ?"
        params.append(business.strip().lower())
    if category:
        sql += " AND LOWER(category) = ?"
        params.append(category.strip().lower())
    if kind:
        sql += " AND kind = ?"
        params.append(normalise_kind(kind))
    if search:
        like = f"%{search.strip().lower()}%"
        sql += " AND (LOWER(party) LIKE ? OR LOWER(note) LIKE ? OR LOWER(category) LIKE ? OR LOWER(ref) LIKE ?)"
        params += [like] * 4
    sql += f" ORDER BY {order}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return db.execute(sql, params)


def _totals(rows: list[tuple]) -> tuple[float, float]:
    inc = sum(r[2] for r in rows if r[4] == "income")
    out = sum(r[2] for r in rows if r[4] == "expense")
    return round(inc, 2), round(out, 2)


def _line(r: tuple) -> str:
    i, ts, amount, cur, kind, cat, party, note, account, ref, business = r
    verb = {"expense": "out", "income": "in", "transfer": "moved"}.get(kind, kind)
    who = (" to " if kind == "expense" else " from " if kind == "income" else " via ") + party if party else ""
    extra = []
    if note:
        extra.append(note)
    if ref:
        extra.append(f"ref {ref}")
    if business and business != "personal":
        extra.append(business)
    tail = f" ({'; '.join(extra)})" if extra else ""
    return f"#{i} {_day(ts)}: {fmt_kes(amount, cur)} {verb}, {cat}{who}{tail}"


def _change(now_v: float, prev_v: float) -> str:
    if prev_v <= 0:
        return "nothing to compare with" if now_v <= 0 else "up from nothing"
    pct = (now_v - prev_v) / prev_v * 100
    if abs(pct) < 1:
        return "about the same"
    return f"{'up' if pct > 0 else 'down'} {abs(pct):.0f}% from {fmt_kes(prev_v)}"


def _net(inc: float, out: float) -> str:
    net = round(inc - out, 2)
    if net > 0:
        return f"{fmt_kes(net)} more in than out"
    if net < 0:
        return f"{fmt_kes(-net)} more out than in"
    return "in and out are even"


def budget_status(db, month_start: dt.datetime, business: str = "") -> list[dict]:
    """Each budget with spending for the calendar month starting at month_start."""
    sql, params = "SELECT category, business, monthly_limit FROM budgets", []
    if business:
        sql += " WHERE LOWER(business) = ?"
        params.append(business.strip().lower())
    out = []
    start = month_start.timestamp()
    end = _month_start(month_start.year, month_start.month + 1).timestamp()
    for cat, biz, limit in db.execute(sql + " ORDER BY business, category", params):
        spent = db.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE kind = 'expense' AND LOWER(category) = ? "
            "AND LOWER(business) = ? AND ts >= ? AND ts < ?", (cat.lower(), biz.lower(), start, end))[0][0]
        out.append({"category": cat, "business": biz, "limit": float(limit), "spent": round(float(spent or 0), 2)})
    return out


def _budget_line(b: dict, multi_business: bool) -> str:
    name = b["category"] + (f" ({b['business']})" if multi_business else "")
    left = b["limit"] - b["spent"]
    pct = b["spent"] / b["limit"] * 100 if b["limit"] else 0
    if left < 0:
        state = f"over by {fmt_kes(-left)}"
    else:
        state = f"{fmt_kes(left)} left"
    return f"{name}: {fmt_kes(b['spent'])} of {fmt_kes(b['limit'])} ({pct:.0f}%), {state}"


# ---------------------------------------------------------------------------------------- tools

@mcp.tool()
def add_transaction(amount: float, kind: str = "expense", category: str = "", party: str = "", note: str = "",
                    account: str = "", when: str = "", business: str = "personal", ref: str = "") -> str:
    """Record money spent or received, in Kenyan shillings (KES).
    kind: expense, income or transfer (moving money between own accounts, e.g. withdrawing cash).
    category: e.g. food, transport, airtime, rent, utilities, supplies, salary, sales; guessed if empty.
    party: who was paid or who paid. account: mpesa (default), cash, bank, or any name.
    when: 'today', 'yesterday', 'yesterday 3pm', '2026-10-19' or '2026-10-19 14:30' (Nairobi time; default now).
    business: 'personal' or a business name, to keep businesses separate. ref: e.g. the M-Pesa code."""
    try:
        amount = float(str(amount).replace(",", "").replace("KES", "").replace("Ksh", "").strip())
    except ValueError:
        raise ValueError(f"'{amount}' is not an amount.")
    if amount <= 0:
        raise ValueError("The amount must be more than zero.")
    k = normalise_kind(kind)
    ts = parse_when_money(when).timestamp()
    cat = (category.strip().lower() or categorise(party, note, k))
    ref = ref.strip().upper()
    db = get_db()
    if _ref_exists(db, ref):
        return f"Already recorded: a transaction with ref {ref} exists."
    tid = _insert(db, ts=ts, amount=amount, kind=k, category=cat, party=party.strip(), note=note.strip(),
                  account=account.strip().lower() or "mpesa", ref=ref, business=business.strip().lower() or "personal")
    verb = {"expense": "spent", "income": "received", "transfer": "moved"}[k]
    who = f" {'to' if k == 'expense' else 'from'} {party.strip()}" if party.strip() and k != "transfer" else ""
    biz = f", {business.strip().lower()}" if business.strip().lower() not in ("", "personal") else ""
    return f"Saved #{tid}: {fmt_kes(amount)} {verb}{who}, {cat}{biz}, {_day(ts)}."


@mcp.tool()
def parse_mpesa(text: str, business: str = "personal", save: bool = True) -> str:
    """Read one or many pasted M-Pesa confirmation SMSes (sent, received, Buy Goods, Pay Bill, withdraw,
    airtime) and record them. Transaction costs are saved as separate 'fees' expenses. Messages already
    recorded (same M-Pesa code) are skipped. save=False only shows what was found."""
    found, skipped = parse_mpesa_messages(text)
    if not found:
        return "I couldn't find any M-Pesa transactions in that." + (f" Skipped: {' | '.join(skipped)}" if skipped else "")
    db = get_db() if save else None
    biz = business.strip().lower() or "personal"
    saved, dupes, lines = 0, 0, []
    now_ts = time.time()
    for t in found:
        desc = f"{fmt_kes(t.amount)} {'in from' if t.kind == 'income' else 'to' if t.kind == 'expense' else 'via'} " \
               f"{t.party or 'unknown'}, {t.category}" + (f", {_day(t.ts)}" if t.ts else "") + \
               (f", plus {fmt_kes(t.cost)} fee" if t.cost else "")
        if not save:
            lines.append(f"{t.ref or 'no code'}: {desc}")
            continue
        if _ref_exists(db, t.ref):
            dupes += 1
            lines.append(f"{t.ref}: already recorded, skipped")
            continue
        ts = t.ts or now_ts
        tid = _insert(db, ts=ts, amount=t.amount, kind=t.kind, category=t.category, party=t.party, note=t.note,
                      account="mpesa", ref=t.ref, business=biz)
        saved += 1
        if t.cost and not _ref_exists(db, f"{t.ref}-FEE" if t.ref else ""):
            _insert(db, ts=ts, amount=t.cost, kind="expense", category="fees", party="Safaricom",
                    note=f"M-Pesa transaction cost{' for ' + t.ref if t.ref else ''}", account="mpesa",
                    ref=f"{t.ref}-FEE" if t.ref else "", business=biz)
        lines.append(f"#{tid} {t.ref or 'no code'}: {desc}")
    if save:
        head = f"Saved {saved} M-Pesa transaction{'s' if saved != 1 else ''}"
        if dupes:
            head += f", skipped {dupes} already recorded"
    else:
        head = f"Found {len(found)} M-Pesa transaction{'s' if len(found) != 1 else ''} (not saved)"
    if skipped:
        head += f", couldn't read {len(skipped)}"
    out = head + ".\n" + "\n".join(lines)
    if skipped:
        out += "\nNot understood: " + " | ".join(skipped)
    return out


@mcp.tool()
def list_transactions(period: str = "this month", category: str = "", kind: str = "", business: str = "",
                      search: str = "", limit: int = 20) -> str:
    """List transactions, newest first. period: 'today', 'yesterday', 'this week', 'last week', 'this month',
    'last month', 'this year', 'last 7 days', 'last 30 days', 'all', '2026-10' or '2026-10-01..2026-10-15'.
    Filter by category, kind (expense/income/transfer), business, or a search word (party, note, ref)."""
    p = parse_period(period)
    db = get_db()
    rows = _query(db, *p.bounds, business=business, category=category, kind=kind, search=search)
    if not rows:
        return f"No transactions for {p.label}" + (" matching that" if category or kind or search or business else "") + "."
    inc, out = _totals(rows)
    n = max(1, min(int(limit or 20), 200))
    head = f"{len(rows)} transaction{'s' if len(rows) != 1 else ''} for {p.label}: {fmt_kes(out)} out, {fmt_kes(inc)} in."
    body = "\n".join(_line(r) for r in rows[:n])
    more = f"\n...and {len(rows) - n} more." if len(rows) > n else ""
    return f"{head}\n{body}{more}"


def summarise(db, period: str = "this month", business: str = "", now: dt.datetime | None = None) -> str:
    p = parse_period(period, now)
    rows = _query(db, *p.bounds, business=business)
    prev = _query(db, *p.prev_bounds, business=business) if p.prev_end > p.prev_start else []
    inc, out = _totals(rows)
    pinc, pout = _totals(prev)
    scope = f" for {business.strip().lower()}" if business else ""
    if not rows:
        lines = [f"No transactions{scope} for {p.label}."]
    else:
        lines = [f"{p.label[0].upper() + p.label[1:]}{scope}: {fmt_kes(out)} spent, {fmt_kes(inc)} received; "
                 f"{_net(inc, out)}."]
        by_cat: dict[str, float] = {}
        for r in rows:
            if r[4] == "expense":
                by_cat[r[5]] = by_cat.get(r[5], 0) + r[2]
        if by_cat and out > 0:
            top = sorted(by_cat.items(), key=lambda kv: -kv[1])[:5]
            lines.append("Top spending: " + ", ".join(f"{c} {fmt_kes(v)} ({v / out * 100:.0f}%)" for c, v in top) + ".")
        big = sorted((r for r in rows if r[4] == "expense"), key=lambda r: -r[2])[:3]
        if big:
            lines.append("Biggest expenses: " + ", ".join(
                f"{fmt_kes(r[2])} {('to ' + r[6]) if r[6] else r[5]} on {_day(r[1])}" for r in big) + ".")
    if p.prev_end > p.prev_start and (rows or prev):
        lines.append(f"Compared with {p.prev_label}: spending {_change(out, pout)}; income {_change(inc, pinc)}.")
    month = dt.datetime.fromtimestamp(min(p.end.timestamp() - 1, _now(now).timestamp()), TZ)
    budgets = budget_status(db, _month_start(month.year, month.month), business)
    if budgets:
        multi = len({b["business"] for b in budgets}) > 1
        lines.append(f"Budgets for {month.strftime('%B')}: " + "; ".join(_budget_line(b, multi) for b in budgets) + ".")
    return "\n".join(lines)


@mcp.tool()
def money_summary(period: str = "this month", business: str = "") -> str:
    """Totals in and out, net, top spending categories with their share, biggest expenses, comparison with
    the previous equal period, and budget status. period as in list_transactions. business: '' means all."""
    return summarise(get_db(), period, business)


@mcp.tool()
def set_budget(category: str, monthly_limit: float, business: str = "personal") -> str:
    """Set (or change) a monthly spending limit for a category, e.g. food 15000. A limit of 0 removes it."""
    cat, biz = category.strip().lower(), business.strip().lower() or "personal"
    if not cat:
        raise ValueError("Which category is the budget for?")
    limit = float(monthly_limit)
    db = get_db()
    if limit <= 0:
        gone = db.execute("DELETE FROM budgets WHERE category = ? AND business = ? RETURNING category", (cat, biz))
        return f"Removed the {cat} budget." if gone else f"There was no {cat} budget."
    db.execute("INSERT INTO budgets (category, business, monthly_limit) VALUES (?, ?, ?) "
               "ON CONFLICT (category, business) DO UPDATE SET monthly_limit = excluded.monthly_limit",
               (cat, biz, limit))
    now = dt.datetime.now(TZ)
    b = next((x for x in budget_status(db, _month_start(now.year, now.month), biz) if x["category"] == cat), None)
    tail = f" So far this month: {_budget_line(b, False).split(': ', 1)[1]}." if b else ""
    return f"Budget set: {cat}{'' if biz == 'personal' else ' (' + biz + ')'} {fmt_kes(limit)} a month.{tail}"


@mcp.tool()
def budgets(business: str = "") -> str:
    """List monthly budgets with how much has been spent this month and what is left."""
    now = dt.datetime.now(TZ)
    rows = budget_status(get_db(), _month_start(now.year, now.month), business)
    if not rows:
        return "No budgets set yet."
    multi = len({b["business"] for b in rows}) > 1
    over = [b for b in rows if b["spent"] > b["limit"]]
    head = f"{len(rows)} budget{'s' if len(rows) != 1 else ''} for {now.strftime('%B')}" + \
           (f", {len(over)} over" if over else ", all within limits") + "."
    return head + "\n" + "\n".join(_budget_line(b, multi) for b in rows)


@mcp.tool()
def delete_transaction(transaction_id: int) -> str:
    """Delete a transaction by its number."""
    gone = get_db().execute("DELETE FROM transactions WHERE id = ? RETURNING id, amount", (int(transaction_id),))
    return f"Deleted #{transaction_id} ({fmt_kes(gone[0][1])})." if gone else f"No transaction #{transaction_id}."


@mcp.tool()
def update_transaction(transaction_id: int, amount: float | None = None, kind: str | None = None,
                       category: str | None = None, party: str | None = None, note: str | None = None,
                       account: str | None = None, when: str | None = None, business: str | None = None,
                       ref: str | None = None) -> str:
    """Change fields of a transaction by its number. Only the fields given are changed."""
    sets: list[str] = []
    params: list = []
    if amount is not None:
        if float(amount) <= 0:
            raise ValueError("The amount must be more than zero.")
        sets.append("amount = ?"); params.append(round(float(amount), 2))
    if kind is not None:
        sets.append("kind = ?"); params.append(normalise_kind(kind))
    if when is not None and when.strip():
        sets.append("ts = ?"); params.append(parse_when_money(when).timestamp())
    for name, val, lower in (("category", category, True), ("party", party, False), ("note", note, False),
                             ("account", account, True), ("business", business, True), ("ref", ref, False)):
        if val is not None:
            v = val.strip().lower() if lower else val.strip()
            if name == "ref":
                v = v.upper()
            sets.append(f"{name} = ?"); params.append(v)
    if not sets:
        return "Nothing to change."
    db = get_db()
    rows = db.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ? RETURNING {_COLS}",
                      params + [int(transaction_id)])
    return f"Updated. {_line(rows[0])}" if rows else f"No transaction #{transaction_id}."


def to_csv(rows: list[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["id", "date", "time", "amount", "currency", "kind", "category", "party", "note", "account",
                "ref", "business"])
    for i, ts, amount, cur, kind, cat, party, note, account, ref, business in rows:
        d = dt.datetime.fromtimestamp(ts, TZ)
        w.writerow([i, d.strftime("%Y-%m-%d"), d.strftime("%H:%M"), f"{amount:.2f}", cur, kind, cat, party, note,
                    account, ref, business])
    return buf.getvalue()


@mcp.tool()
def export_transactions(period: str = "this month", business: str = "") -> str:
    """Return the period's transactions as CSV text (oldest first), ready to save to a file."""
    p = parse_period(period)
    return to_csv(_query(get_db(), *p.bounds, business=business, order="ts ASC, id ASC"))


if __name__ == "__main__":
    mcp.run()
