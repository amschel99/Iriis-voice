"""Money tracker: pure helpers (periods, M-Pesa parsing, categories) and the tools against SQLite
(and Postgres when IRIIS_TEST_PG_URL is set)."""
import csv
import datetime as dt
import io
from zoneinfo import ZoneInfo

import pytest

import servers.money as money
from servers.money import categorise, fmt_kes, parse_mpesa_messages, parse_period, parse_when_money

TZ = ZoneInfo("Africa/Nairobi")
NOW = dt.datetime(2026, 10, 19, 15, 0, tzinfo=TZ)   # a Monday

SENT = ("SJK3ABCD12 Confirmed. Ksh1,200.00 sent to JOHN DOE 0712345678 on 19/10/26 at 2:15 PM. "
        "New M-PESA balance is Ksh5,300.00. Transaction cost, Ksh13.00. Amount you can transact within the day is 498,800.00.")
RECEIVED = ("SJK4EFGH34 Confirmed.You have received Ksh500.00 from JANE WANJIRU 0722000000 on 18/10/26 at 9:30 AM "
            "New M-PESA balance is Ksh5,800.00.")
TILL = ("SJK5IJKL56 Confirmed. Ksh350.00 paid to NAIVAS WESTLANDS. on 17/10/26 at 6:45 PM.New M-PESA balance is "
        "Ksh5,450.00. Transaction cost, Ksh0.00.")
PAYBILL = ("SJK6MNOP78 Confirmed. Ksh2,500.00 sent to KPLC PREPAID for account 37123456789 on 16/10/26 at 8:05 AM "
           "New M-PESA balance is Ksh2,950.00. Transaction cost, Ksh33.00.")
WITHDRAW = ("SJK7QRST90 Confirmed.on 15/10/26 at 9:01 AMWithdraw Ksh2,000.00 from 123456 - MAMA PESA AGENT "
            "Ngong Rd New M-PESA balance is Ksh950.00. Transaction cost, Ksh29.00.")
AIRTIME = "SJK8UVWX12 confirmed. You bought Ksh100.00 of airtime on 14/10/26 at 7:20 AM. New M-PESA balance is Ksh850.00."


# ---------------------------------------------------------------------------------------- pure helpers

def test_fmt_kes():
    assert fmt_kes(1250) == "KES 1,250"
    assert fmt_kes(1250.5) == "KES 1,250.50"
    assert fmt_kes(0) == "KES 0"


@pytest.mark.parametrize("party,note,kind,cat", [
    ("Uber", "", "expense", "transport"),
    ("", "matatu to town", "expense", "transport"),
    ("SHELL KILIMANI", "", "expense", "transport"),
    ("KPLC PREPAID", "", "expense", "utilities"),
    ("", "bought tokens", "expense", "utilities"),
    ("", "safaricom airtime", "expense", "airtime"),
    ("", "data bundles", "expense", "airtime"),
    ("", "lunch at java", "expense", "food"),
    ("NAIVAS", "", "expense", "groceries"),
    ("Landlord", "October rent", "expense", "rent"),
    ("", "school fees for Wanjiku", "expense", "school"),
    ("", "packaging supplies", "expense", "supplies"),
    ("ACME LTD", "salary", "income", "salary"),
    ("", "sale of 3 bags", "income", "sales"),
    ("JOHN DOE", "", "income", "income"),
    ("JOHN DOE", "", "expense", "other"),
    ("", "datapoint", "expense", "other"),     # whole words only
])
def test_categorise(party, note, kind, cat):
    assert categorise(party, note, kind) == cat


def test_parse_when_money():
    assert parse_when_money("", NOW) == NOW
    assert parse_when_money("today", NOW) == NOW
    assert parse_when_money("yesterday", NOW).date() == dt.date(2026, 10, 18)
    assert parse_when_money("yesterday 3pm", NOW) == dt.datetime(2026, 10, 18, 15, 0, tzinfo=TZ)
    assert parse_when_money("2026-10-12", NOW) == dt.datetime(2026, 10, 12, 12, 0, tzinfo=TZ)
    assert parse_when_money("2026-10-12 14:30", NOW) == dt.datetime(2026, 10, 12, 14, 30, tzinfo=TZ)
    assert parse_when_money("19/10/26 2:15 PM", NOW) == dt.datetime(2026, 10, 19, 14, 15, tzinfo=TZ)
    assert parse_when_money("3 days ago", NOW).date() == dt.date(2026, 10, 16)
    with pytest.raises(ValueError):
        parse_when_money("sometime", NOW)


def test_parse_period():
    def d(p):
        x = parse_period(p, NOW)
        return x.start.date(), x.end.date()
    assert d("today") == (dt.date(2026, 10, 19), dt.date(2026, 10, 20))
    assert d("yesterday") == (dt.date(2026, 10, 18), dt.date(2026, 10, 19))
    assert d("this week") == (dt.date(2026, 10, 19), dt.date(2026, 10, 26))
    assert d("last week") == (dt.date(2026, 10, 12), dt.date(2026, 10, 19))
    assert d("this month") == (dt.date(2026, 10, 1), dt.date(2026, 11, 1))
    assert d("last month") == (dt.date(2026, 9, 1), dt.date(2026, 10, 1))
    assert d("this year") == (dt.date(2026, 1, 1), dt.date(2027, 1, 1))
    assert d("last 7 days") == (dt.date(2026, 10, 13), dt.date(2026, 10, 20))
    assert d("last 30 days") == (dt.date(2026, 9, 20), dt.date(2026, 10, 20))
    assert d("2026-02") == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))
    assert d("2026-10-01..2026-10-15") == (dt.date(2026, 10, 1), dt.date(2026, 10, 16))
    with pytest.raises(ValueError):
        parse_period("whenever", NOW)


def test_previous_period_is_equal_length():
    p = parse_period("this month", NOW)            # running: compare to the same point last month
    assert p.prev_start == dt.datetime(2026, 9, 1, tzinfo=TZ)
    assert p.prev_end == dt.datetime(2026, 9, 19, 15, 0, tzinfo=TZ)
    p = parse_period("last month", NOW)            # finished: the whole month before
    assert (p.prev_start.date(), p.prev_end.date()) == (dt.date(2026, 8, 1), dt.date(2026, 9, 1))
    p = parse_period("last week", NOW)
    assert (p.prev_start.date(), p.prev_end.date()) == (dt.date(2026, 10, 5), dt.date(2026, 10, 12))
    p = parse_period("2026-03-01", NOW)
    assert (p.prev_start.date(), p.prev_end.date()) == (dt.date(2026, 2, 28), dt.date(2026, 3, 1))
    p = parse_period("2026-03", NOW)
    assert (p.prev_start.date(), p.prev_end.date()) == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))


def test_parse_mpesa_samples():
    found, skipped = parse_mpesa_messages("\n".join([SENT, RECEIVED, TILL, PAYBILL, WITHDRAW, AIRTIME]))
    assert skipped == []
    by = {t.ref: t for t in found}
    assert list(by) == ["SJK3ABCD12", "SJK4EFGH34", "SJK5IJKL56", "SJK6MNOP78", "SJK7QRST90", "SJK8UVWX12"]

    s = by["SJK3ABCD12"]
    assert (s.amount, s.kind, s.party, s.cost) == (1200.0, "expense", "JOHN DOE", 13.0)
    assert "0712345678" in s.note
    assert dt.datetime.fromtimestamp(s.ts, TZ) == dt.datetime(2026, 10, 19, 14, 15, tzinfo=TZ)

    r = by["SJK4EFGH34"]
    assert (r.amount, r.kind, r.party, r.category, r.cost) == (500.0, "income", "JANE WANJIRU", "income", 0.0)
    assert dt.datetime.fromtimestamp(r.ts, TZ) == dt.datetime(2026, 10, 18, 9, 30, tzinfo=TZ)

    t = by["SJK5IJKL56"]
    assert (t.amount, t.kind, t.party, t.category, t.cost) == (350.0, "expense", "NAIVAS WESTLANDS", "groceries", 0.0)

    p = by["SJK6MNOP78"]
    assert (p.amount, p.kind, p.party, p.category, p.cost) == (2500.0, "expense", "KPLC PREPAID", "utilities", 33.0)
    assert "37123456789" in p.note

    w = by["SJK7QRST90"]
    assert (w.amount, w.kind, w.category, w.cost) == (2000.0, "transfer", "cash withdrawal", 29.0)
    assert w.party.startswith("MAMA PESA AGENT")
    assert dt.datetime.fromtimestamp(w.ts, TZ) == dt.datetime(2026, 10, 15, 9, 1, tzinfo=TZ)

    a = by["SJK8UVWX12"]
    assert (a.amount, a.kind, a.category) == (100.0, "expense", "airtime")


def test_parse_mpesa_skips_garbage_and_never_invents():
    found, skipped = parse_mpesa_messages("hello there\n\nSJK9ZZZZ99 Confirmed. Your M-PESA balance was Ksh950.00")
    assert found == []
    assert len(skipped) == 2
    found, skipped = parse_mpesa_messages("Ksh300.00 paid to JAVA HOUSE.")   # no code, no date
    assert len(found) == 1 and found[0].ref == "" and found[0].ts is None and found[0].category == "food"
    found, _ = parse_mpesa_messages("SJKAFAIL01 Failed. Insufficient funds to send Ksh9,000.00 to JOHN")
    assert found == []


# ---------------------------------------------------------------------------------------- tools on a real DB

@pytest.fixture
def mdb(db, monkeypatch):
    for t in ("transactions", "budgets"):
        db.execute(f"DELETE FROM {t}")
    monkeypatch.setattr(money, "get_db", lambda: db)
    return db


def test_add_transaction(mdb):
    out = money.add_transaction(1250, party="Uber", note="to the office", when="today")
    assert "KES 1,250" in out and "transport" in out
    with pytest.raises(ValueError):
        money.add_transaction(0)
    with pytest.raises(ValueError):
        money.add_transaction(-5)
    with pytest.raises(ValueError):
        money.add_transaction(100, kind="borrowed")
    money.add_transaction(40000, kind="income", category="Sales", business="Seryn", party="Client A", ref="abc123")
    assert "Already recorded" in money.add_transaction(40000, kind="income", ref="ABC123")
    rows = mdb.execute("SELECT kind, category, business, account, ref FROM transactions ORDER BY id")
    assert rows == [("expense", "transport", "personal", "mpesa", ""), ("income", "sales", "seryn", "mpesa", "ABC123")]
    assert "No transactions" not in money.list_transactions(business="seryn")
    assert "Client A" in money.list_transactions(search="client")


def test_parse_mpesa_saves_fees_and_skips_duplicates(mdb):
    out = money.parse_mpesa(SENT + "\n" + PAYBILL, business="seryn")
    assert out.startswith("Saved 2 M-Pesa transactions")
    rows = mdb.execute("SELECT ref, amount, kind, category, business FROM transactions ORDER BY ref")
    assert rows == [("SJK3ABCD12", 1200.0, "expense", "other", "seryn"),
                    ("SJK3ABCD12-FEE", 13.0, "expense", "fees", "seryn"),
                    ("SJK6MNOP78", 2500.0, "expense", "utilities", "seryn"),
                    ("SJK6MNOP78-FEE", 33.0, "expense", "fees", "seryn")]
    again = money.parse_mpesa(SENT + "\n" + PAYBILL + "\n" + RECEIVED + "\n\nrandom words")
    assert "Saved 1" in again and "skipped 2 already recorded" in again and "couldn't read 1" in again
    assert mdb.execute("SELECT COUNT(*) FROM transactions")[0][0] == 5
    preview = money.parse_mpesa(TILL, save=False)
    assert "not saved" in preview and mdb.execute("SELECT COUNT(*) FROM transactions")[0][0] == 5


def _add(db, amount, kind, cat, when, business="personal", party=""):
    money._insert(db, ts=when.timestamp(), amount=amount, kind=kind, category=cat, party=party, note="",
                  account="mpesa", ref="", business=business)


def test_summary_math_and_budgets(mdb):
    oct_ = dt.datetime(2026, 10, 5, 12, 0, tzinfo=TZ)
    sep = dt.datetime(2026, 9, 5, 12, 0, tzinfo=TZ)
    _add(mdb, 6000, "expense", "food", oct_, party="Java")
    _add(mdb, 3000, "expense", "transport", oct_)
    _add(mdb, 1000, "expense", "airtime", oct_)
    _add(mdb, 20000, "income", "salary", oct_)
    _add(mdb, 2000, "transfer", "cash withdrawal", oct_)       # transfers are neither in nor out
    _add(mdb, 9999, "expense", "supplies", oct_, business="seryn")
    _add(mdb, 5000, "expense", "food", sep)
    _add(mdb, 20000, "income", "salary", sep)
    mdb.execute("INSERT INTO budgets (category, business, monthly_limit) VALUES ('food', 'personal', 5000)")
    mdb.execute("INSERT INTO budgets (category, business, monthly_limit) VALUES ('transport', 'personal', 4000)")

    out = money.summarise(mdb, "this month", "personal", now=NOW)
    assert "KES 10,000 spent, KES 20,000 received; KES 10,000 more in than out" in out
    assert "food KES 6,000 (60%), transport KES 3,000 (30%), airtime KES 1,000 (10%)" in out
    assert "Biggest expenses: KES 6,000 to Java" in out
    assert "spending up 100% from KES 5,000" in out and "income about the same" in out
    assert "food: KES 6,000 of KES 5,000 (120%), over by KES 1,000" in out
    assert "transport: KES 3,000 of KES 4,000 (75%), KES 1,000 left" in out
    assert "supplies" not in out                                   # other business excluded

    everything = money.summarise(mdb, "this month", now=NOW)
    assert "KES 19,999 spent" in everything

    rows = money.budget_status(mdb, dt.datetime(2026, 10, 1, tzinfo=TZ), "personal")
    assert {(b["category"], b["spent"], b["limit"]) for b in rows} == {("food", 6000.0, 5000.0), ("transport", 3000.0, 4000.0)}
    assert "No transactions" in money.summarise(mdb, "2026-01", now=NOW)


def test_budget_tools(mdb):
    assert money.budgets() == "No budgets set yet."
    assert "Budget set: food KES 15,000 a month" in money.set_budget("Food", 15000)
    assert "KES 20,000" in money.set_budget("food", 20000)              # upsert, not a second row
    money.set_budget("supplies", 50000, business="seryn")
    money.add_transaction(25000, category="food")
    listing = money.budgets()
    assert "2 budgets" in listing and "1 over" in listing and "over by KES 5,000" in listing
    assert "seryn" in listing
    assert money.set_budget("food", 0) == "Removed the food budget."
    assert mdb.execute("SELECT COUNT(*) FROM budgets")[0][0] == 1


def test_update_delete_export(mdb):
    out = money.add_transaction(500, party="Mama mboga", when="2026-10-10 08:00")
    tid = int(out.split("#")[1].split(":")[0])
    assert "KES 750" in money.update_transaction(tid, amount=750, category="Food", note="sukuma")
    assert mdb.execute("SELECT amount, category, note FROM transactions WHERE id = ?", (tid,)) == [(750.0, "food", "sukuma")]
    assert money.update_transaction(tid) == "Nothing to change."
    assert money.update_transaction(99999, amount=1) == "No transaction #99999."

    text = money.export_transactions("2026-10")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][:4] == ["id", "date", "time", "amount"]
    assert rows[1][1:4] == ["2026-10-10", "08:00", "750.00"]

    assert money.delete_transaction(tid) == f"Deleted #{tid} (KES 750)."
    assert money.delete_transaction(tid) == f"No transaction #{tid}."
