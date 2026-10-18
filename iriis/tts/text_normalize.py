"""Turn LLM text into something a TTS engine reads naturally.

- strips markdown, code and URLs (URLs are returned separately so the UI can show them)
- expands currencies ("KSh 1,500" -> "fifteen hundred shillings"), times, percentages,
  ordinals, decimals, plain numbers and common abbreviations
"""
from __future__ import annotations

import re

from num2words import num2words

ABBREVIATIONS = {
    r"\bDr\.": "Doctor", r"\bMr\.": "Mister", r"\bMrs\.": "Missus", r"\bMs\.": "Miz",
    r"\bProf\.": "Professor", r"\bSt\.": "Street", r"\bAve\.": "Avenue", r"\bRd\.": "Road",
    r"\bapprox\.": "approximately", r"\betc\.": "et cetera", r"\be\.g\.": "for example",
    r"\bi\.e\.": "that is", r"\bvs\.?": "versus", r"\bno\.(?=\s*\d)": "number",
    r"\bkm/h\b": "kilometres per hour", r"\bkm\b": "kilometres", r"\bkg\b": "kilograms",
    r"\bGB\b": "gigabytes", r"\bMB\b": "megabytes", r"\bKB\b": "kilobytes", r"\bTB\b": "terabytes",
    r"\bGHz\b": "gigahertz", r"\bMHz\b": "megahertz", r"°C": " degrees Celsius", r"°F": " degrees Fahrenheit",
    r"\bCBD\b": "C B D", r"\bJKIA\b": "J K I A", r"\bUoN\b": "University of Nairobi",
    r"\bCPU\b": "C P U", r"\bRAM\b": "ram", r"\bAPI\b": "A P I", r"\bUSB\b": "U S B",
}

# symbol/code -> (singular, plural); sub-unit name
CURRENCIES = {
    "ksh": ("shilling", "shillings", "cent"), "kes": ("shilling", "shillings", "cent"),
    "ush": ("Ugandan shilling", "Ugandan shillings", "cent"), "ugx": ("Ugandan shilling", "Ugandan shillings", "cent"),
    "tsh": ("Tanzanian shilling", "Tanzanian shillings", "cent"), "tzs": ("Tanzanian shilling", "Tanzanian shillings", "cent"),
    "$": ("dollar", "dollars", "cent"), "usd": ("dollar", "dollars", "cent"),
    "€": ("euro", "euros", "cent"), "eur": ("euro", "euros", "cent"),
    "£": ("pound", "pounds", "penny"), "gbp": ("pound", "pounds", "penny"),
}

URL_RE = re.compile(r"(https?://[^\s)>\]]+|www\.[^\s)>\]]+)", re.I)
_NUM = r"\d[\d,]*(?:\.\d+)?"


def _to_number(s: str) -> float | int:
    s = s.replace(",", "")
    return float(s) if "." in s else int(s)


def say_number(n: float | int) -> str:
    """Natural spoken form. Round hundreds between 1100 and 9900 become "fifteen hundred"."""
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if isinstance(n, int) and 1100 <= n <= 9900 and n % 100 == 0 and n % 1000 != 0:
        return f"{num2words(n // 100)} hundred"
    if isinstance(n, float):
        whole, frac = f"{n}".split(".")
        return f"{num2words(int(whole))} point {' '.join(num2words(int(d)) for d in frac)}"
    return num2words(n).replace(",", "")


def _say_currency(sym: str, amount_text: str, scale: str | None) -> str | None:
    names = CURRENCIES.get(sym.lower().strip(". "))
    if names is None:
        return None
    singular, plural, sub = names
    amount = _to_number(amount_text)
    amount *= {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}.get((scale or "").lower(), 1)
    whole = int(amount)
    cents = round((amount - whole) * 100)
    out = f"{say_number(whole)} {singular if whole == 1 else plural}"
    if cents:
        sub_plural = "pence" if sub == "penny" else sub + "s"
        out += f" and {num2words(cents)} {sub if cents == 1 else sub_plural}"
    return out


_CURRENCY_RE = re.compile(
    r"(?:(?P<pre>KSh\.?|KES|USh\.?|UGX|TSh\.?|TZS|USD|EUR|GBP|[$€£])\s?(?P<amt>" + _NUM + r")"
    r"|(?P<amt2>" + _NUM + r")\s?(?P<post>KSh|KES|USD|EUR|GBP|shillings|bob)\b)"
    r"(?:\s?(?P<scale>k|m|bn|thousand|million|billion)\b)?",
    re.I,
)


def _currency(m: re.Match) -> str:
    if m.group("pre"):
        sym, amt = m.group("pre"), m.group("amt")
    else:
        sym, amt = m.group("post"), m.group("amt2")
        if sym.lower() in ("shillings", "bob"):
            sym = "ksh"
    return _say_currency(sym, amt, m.group("scale")) or m.group(0)


def _time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    suffix = (m.group(3) or "").lower().replace(".", "")
    if h > 23 or mi > 59:
        return m.group(0)
    hour_word = num2words(h if suffix or h <= 12 else h)
    if mi == 0:
        minute = "" if suffix else " hundred" if h > 12 else " o'clock"
    elif mi < 10:
        minute = f" oh {num2words(mi)}"
    else:
        minute = f" {num2words(mi)}"
    spoken_suffix = {"am": " A M", "pm": " P M"}.get(suffix, "")
    return f"{hour_word}{minute}{spoken_suffix}"


def _ordinal(m: re.Match) -> str:
    return num2words(int(m.group(1)), to="ordinal")


def _plain_number(m: re.Match) -> str:
    raw = m.group(0)
    try:
        n = _to_number(raw)
    except ValueError:
        return raw
    if isinstance(n, int) and 1900 <= n <= 2099 and "," not in raw:
        return num2words(n, to="year")
    return say_number(n)


def strip_markdown(text: str) -> str:
    text = re.sub(r"```[\s\S]*?```", " I've put the code on screen. ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*(?:[-*+•]|\d+[.)])\s+", "", text)
    text = re.sub(r"(\*\*|__|\*|_|~~)(\S(?:.*?\S)?)\1", r"\2", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = re.sub(r"\|", ", ", text)
    return text


def extract_urls(text: str) -> tuple[str, list[str]]:
    urls = URL_RE.findall(text)
    if not urls:
        return text, []
    text = URL_RE.sub("", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not re.search(r"on screen", text, re.I):
        text = (text + " I've put the link on screen.").strip()
    return text, urls


# Real acronyms stay capitalised (spelled out); every other ALL-CAPS word is shouting, not an acronym.
ACRONYMS = {"AI", "TV", "PC", "OK", "ID", "UK", "EU", "UN", "SGR", "CBD", "JKIA", "USD", "KSH", "KES",
            "CPU", "GPU", "RAM", "API", "USB", "URL", "PDF", "SMS", "GPS", "ATM", "VPN", "MCP", "LLM", "TTS", "STT", "CEO", "NGO"}
_CAPS_WORD = re.compile(r"\b[A-Z][A-Z']+\b")


def calm_shouting(text: str) -> str:
    """'SIT THE FUCK DOWN' -> 'sit the fuck down' so TTS says words instead of spelling letters.
    Punctuation (! ?! ...) is kept, so the delivery still carries the emphasis."""
    def calm_sentence(sentence: str) -> str:
        words = re.findall(r"[A-Za-z']+", sentence)
        caps = _CAPS_WORD.findall(sentence)
        shouty = len(words) > 0 and len(caps) / len(words) >= 0.4   # judged per sentence

        def fix(m: re.Match) -> str:
            w = m.group(0)
            if w in ACRONYMS:
                return w
            if len(w) >= 3 or shouty:
                return w.lower() if w != "I" else w
            return w

        return _CAPS_WORD.sub(fix, sentence)

    return "".join(calm_sentence(s) for s in re.split(r"(?<=[.!?])(?=\s)", text))


DIRECTION_RE = re.compile(r"\[([A-Za-z][A-Za-z ,'-]{0,38})\]")


def strip_directions(text: str) -> str:
    """Remove vocal directions like [angry] (for display and for voices that can't act)."""
    return re.sub(r"\s{2,}", " ", DIRECTION_RE.sub("", text)).strip()


def is_shouting(text: str) -> bool:
    words = re.findall(r"[A-Za-z']+", DIRECTION_RE.sub("", text))
    caps = [w for w in words if len(w) > 1 and w.isupper() and w not in ACRONYMS]
    return len(words) >= 2 and len(caps) / len(words) >= 0.5


def normalize(text: str, currency: str = "shillings", keep_directions: bool = False) -> str:
    """keep_directions=True (expressive voices): keep [angry]-style directions, and turn an
    all-caps sentence into a [shouting] one. Otherwise directions are removed."""
    directions: list[str] = []
    if keep_directions:
        if is_shouting(text) and not DIRECTION_RE.search(text):
            text = "[shouting] " + text

        def stash(m: re.Match) -> str:
            # letters only: digits would be turned into words by the number step
            directions.append(m.group(0).lower())
            return f" qqdir{_tag(len(directions) - 1)}qq "
        text = DIRECTION_RE.sub(stash, text)
    else:
        text = DIRECTION_RE.sub(" ", text)
    text = _normalize_plain(text, currency)
    for i, d in enumerate(directions):
        text = re.sub(rf"\s*qqdir{_tag(i)}qq\s*", f" {d} ", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()


def _tag(i: int) -> str:
    return "".join(chr(ord("a") + int(c)) for c in str(i))   # 0 -> "a", 12 -> "bc"


def _normalize_plain(text: str, currency: str = "shillings") -> str:
    text = strip_markdown(text)
    text, _ = extract_urls(text)
    for pat, repl in ABBREVIATIONS.items():
        text = re.sub(pat, repl, text)
    text = calm_shouting(text)   # after abbreviations, so "16 GB" still becomes "gigabytes"
    text = _CURRENCY_RE.sub(_currency, text)
    text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\s?([ap]\.?m\.?)?(?!\d)", _time, text, flags=re.I)
    text = re.sub(r"\b(1[0-2]|[1-9])\s?([ap])\.?m\.?\b", lambda m: f"{num2words(int(m.group(1)))} {m.group(2).upper()} M", text, flags=re.I)
    text = re.sub(r"(" + _NUM + r")\s?%", lambda m: f"{say_number(_to_number(m.group(1)))} percent", text)
    text = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", _ordinal, text)
    text = re.sub(r"(?<![\w.])-(" + _NUM + r")\b", lambda m: "minus " + m.group(1), text)
    text = re.sub(_NUM, _plain_number, text)
    text = text.replace("&", " and ").replace("+", " plus ").replace("=", " equals ")
    text = re.sub(r"[*#_~<>\[\]{}]", " ", text)
    return re.sub(r"\s{2,}", " ", text).strip()
