"""apps_and_media MCP server: apps, volume, media keys, screenshots, lock screen.

Platform-specific work lives in a small backend class per OS behind one interface.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

import psutil

from servers._common import make_server

mcp = make_server("apps_and_media")

# Friendly names -> executable / launch command, per platform.
WIN_APPS = {
    "notepad": "notepad", "calculator": "calc", "paint": "mspaint", "explorer": "explorer", "file explorer": "explorer",
    "terminal": "wt", "powershell": "powershell", "command prompt": "cmd", "task manager": "taskmgr",
    "settings": "ms-settings:", "chrome": "chrome", "edge": "msedge", "firefox": "firefox", "vs code": "code",
    "vscode": "code", "spotify": "spotify:", "word": "winword", "excel": "excel", "outlook": "outlook",
    "teams": "msteams:", "camera": "microsoft.windows.camera:", "snipping tool": "snippingtool", "control panel": "control",
}
MAC_APPS = {"vs code": "Visual Studio Code", "vscode": "Visual Studio Code", "chrome": "Google Chrome", "terminal": "Terminal"}
LINUX_APPS = {"vs code": "code", "vscode": "code", "chrome": "google-chrome", "terminal": "x-terminal-emulator", "files": "xdg-open ~", "calculator": "gnome-calculator"}


class Backend:
    def open_app(self, name: str) -> str: raise NotImplementedError
    def media_key(self, key: str) -> None: raise NotImplementedError
    def volume(self, action: str, steps: int) -> None: raise NotImplementedError
    def lock(self) -> None: raise NotImplementedError


class WindowsBackend(Backend):
    VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "mute": 0xAD, "down": 0xAE, "up": 0xAF}

    def _press(self, vk: int, times: int = 1) -> None:
        import ctypes
        for _ in range(times):
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)

    def open_app(self, name: str) -> str:
        target = WIN_APPS.get(name.lower(), name)
        if target.endswith(":"):
            os.startfile(target)  # noqa: S606  (URI protocol handlers like ms-settings:)
        else:
            subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=subprocess.CREATE_NO_WINDOW)
        return target

    def media_key(self, key: str) -> None:
        self._press(self.VK[key])

    def volume(self, action: str, steps: int) -> None:
        self._press(self.VK[action], 1 if action == "mute" else steps)  # each step is 2%

    def lock(self) -> None:
        import ctypes
        ctypes.windll.user32.LockWorkStation()


class MacBackend(Backend):
    def open_app(self, name: str) -> str:
        app = MAC_APPS.get(name.lower(), name)
        subprocess.Popen(["open", "-a", app])
        return app

    def media_key(self, key: str) -> None:
        cmd = {"play_pause": "playpause", "next": "next track", "previous": "previous track", "stop": "pause"}[key]
        subprocess.run(["osascript", "-e", f'tell application "Music" to {cmd}'], check=False)

    def volume(self, action: str, steps: int) -> None:
        script = {"mute": "set volume output muted not (output muted of (get volume settings))",
                  "up": f"set volume output volume ((output volume of (get volume settings)) + {steps * 2})",
                  "down": f"set volume output volume ((output volume of (get volume settings)) - {steps * 2})"}[action]
        subprocess.run(["osascript", "-e", script], check=False)

    def lock(self) -> None:
        subprocess.run(["pmset", "displaysleepnow"], check=False)


class LinuxBackend(Backend):
    def open_app(self, name: str) -> str:
        app = LINUX_APPS.get(name.lower(), name)
        subprocess.Popen(app, shell=True, start_new_session=True)  # noqa: S602 (app names come from the user)
        return app

    def media_key(self, key: str) -> None:
        cmd = {"play_pause": "play-pause", "next": "next", "previous": "previous", "stop": "stop"}[key]
        subprocess.run(["playerctl", cmd], check=False)

    def volume(self, action: str, steps: int) -> None:
        arg = {"mute": "toggle", "up": f"{steps * 2}%+", "down": f"{steps * 2}%-"}[action]
        if shutil.which("wpctl"):
            sub = ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"] if action == "mute" else \
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", arg.replace("%", "%")]
            subprocess.run(sub, check=False)
        else:
            subprocess.run(["amixer", "-q", "set", "Master", arg], check=False)

    def lock(self) -> None:
        subprocess.run(["loginctl", "lock-session"], check=False)


backend: Backend = WindowsBackend() if sys.platform == "win32" else MacBackend() if sys.platform == "darwin" else LinuxBackend()


@mcp.tool()
def open_app(name: str) -> str:
    """Open an application by name, e.g. 'notepad', 'chrome', 'calculator', 'vs code', 'spotify'."""
    if re.search(r"[;&|`$<>]", name):
        return "That doesn't look like an application name."
    target = backend.open_app(name.strip())
    return f"Opening {name} ({target})."


@mcp.tool()
def close_app(name: str) -> str:
    """Close (terminate) all running processes whose name matches, e.g. 'notepad' or 'chrome'."""
    key = WIN_APPS.get(name.lower(), name).lower().rstrip(":")
    protected = {"explorer", "system", "csrss", "winlogon", "services", "lsass", "svchost", "python", "ollama"}
    victims = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().removesuffix(".exe") in (key, name.lower())]
    victims = [p for p in victims if (p.info["name"] or "").lower().removesuffix(".exe") not in protected]
    if not victims:
        return f"No running app called {name}."
    for p in victims:
        try:
            p.terminate()
        except psutil.Error:
            pass
    return f"Closed {len(victims)} {name} process(es)."


@mcp.tool()
def media_control(action: str) -> str:
    """Control media playback. action: play_pause, next, previous or stop."""
    if action not in ("play_pause", "next", "previous", "stop"):
        return "action must be play_pause, next, previous or stop"
    backend.media_key(action)
    return f"Media: {action.replace('_', '/')}."


@mcp.tool()
def set_volume(action: str, steps: int = 5) -> str:
    """Change system volume. action: up, down or mute (mute toggles). Each step is about 2 percent."""
    if action not in ("up", "down", "mute"):
        return "action must be up, down or mute"
    backend.volume(action, max(1, min(steps, 50)))
    return f"Volume {action}."


@mcp.tool()
def take_screenshot() -> str:
    """Take a screenshot of the whole screen and save it to Pictures/Screenshots."""
    folder = Path.home() / "Pictures" / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"iriis-{dt.datetime.now():%Y%m%d-%H%M%S}.png"
    if sys.platform == "win32":
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($bmp);$g.CopyFromScreen($b.Left,$b.Top,0,0,$bmp.Size);"
            f"$bmp.Save('{path}',[System.Drawing.Imaging.ImageFormat]::Png)"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    elif sys.platform == "darwin":
        subprocess.run(["screencapture", "-x", str(path)], check=True, timeout=20)
    else:
        tool = next((t for t in ("gnome-screenshot", "scrot", "grim") if shutil.which(t)), None)
        if not tool:
            return "No screenshot tool found (install gnome-screenshot, scrot or grim)."
        subprocess.run([tool, "-f", str(path)] if tool == "gnome-screenshot" else [tool, str(path)], check=True, timeout=20)
    return f"Screenshot saved to {path}"


@mcp.tool()
def lock_screen() -> str:
    """Lock the computer screen."""
    backend.lock()
    return "Screen locked."


@mcp.tool()
def open_url(url: str) -> str:
    """Open a web address in the default browser."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opened {url}"


if __name__ == "__main__":
    mcp.run()
