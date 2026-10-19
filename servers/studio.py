"""studio MCP server: charts, diagrams and documents made in the cloud, kept in the database, sent anywhere.

    make_chart       bar / line / area / pie / donut / scatter / histogram charts (Matplotlib)
    make_diagram     flowcharts, org charts, mind maps, process and system maps from Graphviz DOT
    make_document    PDF / Word / Excel / PowerPoint from simple Markdown, with charts and diagrams inside
    send_file        to the user's Telegram (photo or document)
    list_files       what's been made recently

Every file is stored in the `files` table (Railway Postgres), so it isn't tied to any computer; the desktop's
documents server can save one to the PC (save_from_cloud) and open it. All libraries are open source:
Matplotlib (PSF-style), Graphviz (EPL), python-docx / openpyxl / python-pptx (MIT), ReportLab (BSD).
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import tempfile
import time
from pathlib import Path

import httpx

from servers._common import make_server
from iriis.storage import get_db

mcp = make_server("studio")
WORK = Path(tempfile.mkdtemp(prefix="iriis-studio-"))
os.environ["IRIIS_ALLOWED_DIRS"] = json.dumps([str(WORK)])    # the documents builders write here, then we store
MAX_BYTES = 15 * 1024 * 1024
for gv in (r"C:\Program Files\Graphviz\bin", r"C:\Program Files (x86)\Graphviz\bin"):
    if os.path.isdir(gv) and gv not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + gv

PALETTE = ["#2E86C1", "#E67E22", "#27AE60", "#8E44AD", "#C0392B", "#16A085", "#F1C40F", "#7F8C8D", "#D35400", "#2C3E50"]


# ---------------------------------------------------------------------------- storage
def _slug(name: str, ext: str) -> str:
    base = re.sub(r"[^\w\- ]+", "", Path(name or "").stem).strip().replace(" ", "_")[:60] or f"iriis_{int(time.time())}"
    return f"{base}{ext}"


def store(name: str, data: bytes, mime: str | None = None) -> int:
    if len(data) > MAX_BYTES:
        raise ValueError(f"That file is {len(data) / 1e6:.1f} MB; the limit is 15 MB.")
    mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return get_db().insert("INSERT INTO files (name, mime, size, data, created) VALUES (?, ?, ?, ?, ?)",
                           (name, mime, len(data), base64.b64encode(data).decode(), time.time()))


def load(file_id: int) -> tuple[str, str, bytes]:
    rows = get_db().execute("SELECT name, mime, data FROM files WHERE id = ?", (int(file_id),))
    if not rows:
        raise ValueError(f"There's no file {file_id}. list_files shows what I've made.")
    name, mime, data = rows[0]
    return name, mime, base64.b64decode(data)


def _ready(fid: int, name: str, size: int, what: str) -> str:
    return (f"{what} ready: file {fid}, {name} ({size / 1024:.0f} KB). Send it with send_file, put it in a document "
            f"with ![caption](file:{fid}), or save it to the computer with save_from_cloud.")


# ---------------------------------------------------------------------------- charts
def render_chart(kind: str, title: str, labels: list, series: dict[str, list[float]], x_label: str = "",
                 y_label: str = "", stacked: bool = False, horizontal: bool = False, show_values: bool = False,
                 unit: str = "") -> bytes:
    import io

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    kind = kind.lower().strip().replace(" ", "_")
    names = list(series)
    if not names:
        raise ValueError("series is empty: give e.g. {\"Sales\": [120, 150, 90]}.")
    vals = {k: [float(v) for v in series[k]] for k in names}
    n = max(len(v) for v in vals.values())
    labels = [str(x) for x in (labels or list(range(1, n + 1)))]
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.alpha": 0.25, "axes.titleweight": "bold", "axes.titlesize": 14})
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=160)
    def fmt(v: float) -> str:
        return f"{unit}{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{unit}{v:,.1f}"
    if kind in ("pie", "donut"):
        k = names[0]
        wedges, *_ = ax.pie(vals[k], labels=labels, autopct="%1.0f%%", startangle=90, colors=PALETTE[:len(labels)],
                            wedgeprops={"width": 0.45} if kind == "donut" else None, pctdistance=0.78 if kind == "donut" else 0.6)
        ax.axis("equal")
        ax.grid(False)
    elif kind in ("bar", "column", "stacked_bar", "barh"):
        stacked = stacked or kind == "stacked_bar"
        horizontal = horizontal or kind == "barh"
        x = np.arange(len(labels))
        width = 0.8 if stacked or len(names) == 1 else 0.8 / len(names)
        bottom = np.zeros(len(labels))
        for i, k in enumerate(names):
            v = np.array(vals[k] + [0] * (len(labels) - len(vals[k])))
            pos = x if stacked or len(names) == 1 else x - 0.4 + width * (i + 0.5)
            bars = (ax.barh(pos, v, width, left=bottom, label=k, color=PALETTE[i % 10]) if horizontal
                    else ax.bar(pos, v, width, bottom=bottom, label=k, color=PALETTE[i % 10]))
            if show_values:
                ax.bar_label(bars, labels=[fmt(t) for t in v], fontsize=9, padding=2,
                             label_type="center" if stacked else "edge")
            if stacked:
                bottom = bottom + v
        (ax.set_yticks if horizontal else ax.set_xticks)(x, labels, **({} if horizontal else {"rotation": 30 if len(labels) > 6 else 0}))
        ax.grid(axis="x" if not horizontal else "y", visible=False)
    elif kind in ("line", "area"):
        x = np.arange(len(labels))
        bottom = np.zeros(len(labels))
        for i, k in enumerate(names):
            v = np.array(vals[k] + [np.nan] * (len(labels) - len(vals[k])))
            if kind == "area":
                top = bottom + np.nan_to_num(v) if stacked else np.nan_to_num(v)
                ax.fill_between(x, bottom if stacked else 0, top, alpha=0.35, color=PALETTE[i % 10], label=k)
                ax.plot(x, top, color=PALETTE[i % 10], lw=1.8)
                bottom = top if stacked else bottom
            else:
                ax.plot(x, v, marker="o" if len(labels) <= 24 else None, lw=2.2, color=PALETTE[i % 10], label=k)
                if show_values:
                    for xi, yi in zip(x, v):
                        if not np.isnan(yi):
                            ax.annotate(fmt(yi), (xi, yi), textcoords="offset points", xytext=(0, 6), ha="center", fontsize=8)
        step = max(1, len(labels) // 12)
        ax.set_xticks(x[::step], labels[::step], rotation=30 if len(labels) > 8 else 0)
    elif kind == "scatter":
        for i, k in enumerate(names):
            pts = series[k]
            if pts and isinstance(pts[0], (list, tuple)):
                xs, ys = zip(*pts)
            else:
                xs, ys = [float(l) for l in labels[:len(pts)]], pts
            ax.scatter(xs, ys, s=40, alpha=0.8, color=PALETTE[i % 10], label=k)
    elif kind in ("histogram", "hist"):
        for i, k in enumerate(names):
            ax.hist(vals[k], bins="auto", alpha=0.6 if len(names) > 1 else 0.9, color=PALETTE[i % 10], label=k)
    else:
        raise ValueError(f"Unknown chart kind '{kind}'. Use bar, stacked_bar, barh, line, area, pie, donut, scatter or histogram.")
    if title:
        ax.set_title(title)
    if x_label and kind not in ("pie", "donut"):
        ax.set_xlabel(x_label)
    if y_label and kind not in ("pie", "donut"):
        ax.set_ylabel(y_label)
    if kind not in ("pie", "donut") and unit:
        from matplotlib.ticker import FuncFormatter
        (ax.xaxis if horizontal else ax.yaxis).set_major_formatter(FuncFormatter(lambda v, _: f"{unit}{v:,.0f}"))
    if len(names) > 1 and kind not in ("pie", "donut"):
        ax.legend(frameon=False)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


@mcp.tool()
def make_chart(kind: str, title: str, series: dict[str, list[float]], labels: list[str] | None = None,
               x_label: str = "", y_label: str = "", stacked: bool = False, horizontal: bool = False,
               show_values: bool = False, unit: str = "", filename: str = "") -> str:
    """Draw a chart as a PNG. kind: bar, stacked_bar, barh, line, area, pie, donut, scatter, histogram.
    series: {name: values}, e.g. {"Sales": [120, 150, 90], "Costs": [80, 95, 70]}; labels: the categories or
    x values, e.g. ["Jan", "Feb", "Mar"] (pie/donut use the first series). unit: prefix for numbers, e.g. "KES ".
    Only plot numbers you actually have (from tools or the user); never invent data."""
    png = render_chart(kind, title, labels or [], series, x_label, y_label, stacked, horizontal, show_values, unit)
    name = _slug(filename or title, ".png")
    return _ready(store(name, png, "image/png"), name, len(png), "Chart")


# ---------------------------------------------------------------------------- diagrams
@mcp.tool()
def make_diagram(dot: str, filename: str = "", engine: str = "dot", fmt: str = "png") -> str:
    """Draw a diagram from Graphviz DOT, e.g. 'digraph { rankdir=LR; node [shape=box, style="rounded,filled",
    fillcolor="#EAF2F8"]; Order -> Pack -> Deliver; }'. Good for flowcharts, processes, org charts, systems,
    timelines and mind maps. engine: dot (flows, hierarchies), neato or fdp (networks), twopi or circo (mind maps,
    rings). fmt: png, svg or pdf."""
    import graphviz
    fmt = fmt.lower() if fmt.lower() in ("png", "svg", "pdf") else "png"
    src = dot.strip()
    if not re.match(r"^(strict\s+)?(di)?graph\b", src, re.I):
        src = "digraph {\n" + src + "\n}"
    if fmt == "png" and "dpi" not in src:
        src = re.sub(r"\{", "{\n  graph [dpi=160, fontname=\"Helvetica\"]; node [fontname=\"Helvetica\"]; "
                            "edge [fontname=\"Helvetica\"];", src, count=1)
    try:
        data = graphviz.Source(src, engine=engine if engine in ("dot", "neato", "fdp", "twopi", "circo", "sfdp") else "dot").pipe(format=fmt)
    except graphviz.backend.execute.CalledProcessError as e:
        err = (e.stderr or b"").decode(errors="replace").strip().splitlines()
        raise ValueError(f"The diagram has a mistake: {err[-1] if err else e}. Fix the DOT and try again.") from e
    except graphviz.backend.execute.ExecutableNotFound as e:
        raise ValueError("Graphviz isn't installed where I run, so I can't draw diagrams right now.") from e
    name = _slug(filename or "diagram", f".{fmt}")
    return _ready(store(name, data), name, len(data), "Diagram")


# ---------------------------------------------------------------------------- documents
def _cloud_image(ref: str) -> Path:
    """![caption](file:12) -> that stored chart/diagram, written to the work folder for the builders."""
    m = re.fullmatch(r"(?:file:)?(\d+)", ref.strip())
    if not m:
        raise ValueError(f"Images in cloud documents are my own files: write ![caption](file:12), not {ref}.")
    name, _, data = load(int(m.group(1)))
    p = WORK / f"img_{m.group(1)}_{name}"
    p.write_bytes(data)
    return p


@mcp.tool()
def make_document(kind: str, filename: str, content: str = "", title: str = "",
                  sheets: dict[str, list[list]] | None = None, slides: list[dict] | None = None) -> str:
    """Make a document in the cloud (works with the computer off). kind: pdf, docx (Word), xlsx (Excel) or pptx
    (PowerPoint). pdf/docx: content in simple Markdown (# headings, - bullets, **bold**, | tables |) and charts or
    diagrams on their own line as ![caption](file:12). xlsx: sheets {name: rows}. pptx: slides [{"title",
    "bullets", "image": "file:12", "notes"}]. Then send_file or save_from_cloud."""
    import servers.documents as D
    D.resolve_image = _cloud_image
    kind = kind.lower().strip(". ").replace("word", "docx").replace("excel", "xlsx").replace("powerpoint", "pptx")
    name = _slug(filename or title, f".{kind}")
    for old in WORK.glob(name):
        old.unlink()
    try:
        if kind == "pdf":
            D.create_pdf(name, content, title)
        elif kind == "docx":
            D.create_document(name, content, title)
        elif kind == "xlsx":
            D.create_spreadsheet(name, sheets or {})
        elif kind == "pptx":
            D.create_presentation(name, slides or [])
        else:
            raise ValueError("kind must be pdf, docx, xlsx or pptx.")
    except Exception as e:  # noqa: BLE001
        raise ValueError(str(e).split("\n")[0]) from e
    data = (WORK / name).read_bytes()
    return _ready(store(name, data), name, len(data), {"pdf": "PDF", "docx": "Word document", "xlsx": "Spreadsheet",
                                                      "pptx": "Presentation"}[kind])


# ---------------------------------------------------------------------------- delivery
@mcp.tool()
def send_file(file_id: int, to: str = "telegram", caption: str = "") -> str:
    """Send a file I made (chart, diagram, document) to the user. to: "telegram" (pictures arrive as photos,
    everything else as a document). To put it on the computer, use the documents tool save_from_cloud instead."""
    name, mime, data = load(file_id)
    if to.lower().strip() != "telegram":
        raise ValueError("I can send files to Telegram; for the computer use save_from_cloud.")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        raise ValueError("Telegram isn't set up (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID on Railway).")
    photo = mime in ("image/png", "image/jpeg") and len(data) < 9_500_000
    method, field = ("sendPhoto", "photo") if photo else ("sendDocument", "document")
    for chat in chats:
        r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", timeout=60,
                       data={"chat_id": chat, "caption": caption[:1000]}, files={field: (name, data, mime)})
        if r.status_code != 200 or not r.json().get("ok"):
            raise ValueError(f"Telegram refused it: {r.json().get('description', r.status_code)}")
    return f"Sent {name} to Telegram."


@mcp.tool()
def list_files(limit: int = 15) -> str:
    """Charts, diagrams and documents I've made recently, newest first (id, name, size, when)."""
    rows = get_db().execute("SELECT id, name, size, created FROM files ORDER BY id DESC LIMIT ?", (max(1, min(limit, 50)),))
    if not rows:
        return "I haven't made any files yet."
    import datetime as dt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(os.environ.get("IRIIS_TIMEZONE", "Africa/Nairobi"))
    return "\n".join(f"file {i}: {n} ({s / 1024:.0f} KB, {dt.datetime.fromtimestamp(c, tz).strftime('%d %b %H:%M')})"
                     for i, n, s, c in rows)


if __name__ == "__main__":
    mcp.run()
