"""Console UI (rich): state, live transcript, tool calls, per-stage latency."""
from __future__ import annotations

from rich.console import Console
from rich.table import Table

STATE_STYLE = {
    "idle": ("dim", "○ idle"),
    "listening": ("bold green", "● listening"),
    "thinking": ("bold yellow", "◐ thinking"),
    "speaking": ("bold cyan", "◉ speaking"),
    "confirm": ("bold magenta", "? waiting for confirmation"),
}


class ConsoleUI:
    def __init__(self, show_latency: bool = True):
        self.console = Console(highlight=False)
        self.show_latency = show_latency
        self.state = "idle"
        self._speaking_line_open = False

    def _end_line(self) -> None:
        if self._speaking_line_open:
            self.console.print()
            self._speaking_line_open = False

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._end_line()
        style, label = STATE_STYLE.get(state, ("white", state))
        self.console.print(f"[{style}]{label}[/]")

    def banner(self, text: str) -> None:
        self.console.rule(f"[bold]{text}")

    def info(self, text: str) -> None:
        self._end_line()
        self.console.print(f"[dim]{text}[/]")

    def warn(self, text: str) -> None:
        self._end_line()
        self.console.print(f"[yellow]! {text}[/]")

    def error(self, text: str) -> None:
        self._end_line()
        self.console.print(f"[bold red]✗ {text}[/]")

    def partial_transcript(self, text: str) -> None:
        self.console.print(f"[dim]… {text}[/]", end="\r")

    def user(self, text: str) -> None:
        self._end_line()
        self.console.print(f"[bold green]You:[/] {text}")

    def assistant_sentence(self, text: str) -> None:
        if not self._speaking_line_open:
            self.console.print("[bold cyan]Iriis:[/] ", end="")
            self._speaking_line_open = True
        self.console.print(text + " ", end="")

    def show(self, kind: str, text: str) -> None:
        self._end_line()
        if kind == "screen":
            self.console.print(f"[bold blue]On screen:[/] {text}")
        elif kind == "tool":
            self.console.print(f"  [magenta]{_esc(text)}[/]")
        else:
            self.console.print(text)

    def latency(self, timings: dict[str, float]) -> None:
        if not self.show_latency or not timings:
            return
        self._end_line()
        t = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))
        for k in timings:
            t.add_column(k, justify="right")
        t.add_row(*[f"{v:.0f} ms" for v in timings.values()])
        self.console.print(t)


def _esc(text: str) -> str:
    return text.replace("[", r"\[")
