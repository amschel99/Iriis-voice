"""`iriis`: control Iriis from Git Bash (or any terminal).

    iriis setup                 save your Railway URL + keys to .env (generates tokens if you like)
    iriis start                 run the desktop agent + voice in the background
    iriis stop | restart
    iriis status [--say]        is she running, and is she connected to her brain on Railway?
    iriis ask "question"        talk to her from the terminal (streams the reply; --speak to hear it)
    iriis say "text"            have her say something
    iriis logs [-f]             show the background agent's log
    iriis server                run the cloud core locally (development)
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from iriis.config import ROOT, resolve_path
from iriis.envfile import ENV_PATH, load_env, read_env, to_http, write_env

PID_FILE = resolve_path("data/iriis.pid")
LOG_FILE = resolve_path("logs/desktop.out")
STATUS_FILE = resolve_path("data/desktop_status.json")

PURPLE, GREEN, YELLOW, RED, DIM, BOLD, RESET = ("\033[95m", "\033[92m", "\033[93m", "\033[91m", "\033[2m", "\033[1m", "\033[0m") \
    if sys.stdout.isatty() or os.environ.get("TERM") else ("",) * 7


def iriis_says(text: str) -> None:
    print(f"{PURPLE}{BOLD}Iriis:{RESET} {text}")


def _server_url() -> str:
    url = os.environ.get("IRIIS_SERVER_URL", "")
    if not url:
        sys.exit(f"{RED}No brain URL configured.{RESET} Run: iriis setup")
    return to_http(url)


def _auth() -> dict[str, str]:
    key = os.environ.get("IRIIS_API_KEY", "")
    return {"Authorization": f"Bearer {key}"} if key else {}


# ---------------------------------------------------------------------------- process helpers

def _running_pid() -> int | None:
    import psutil
    try:
        pid = int(PID_FILE.read_text().strip())
        proc = psutil.Process(pid)
        if proc.is_running() and any("iriis.desktop" in part for part in proc.cmdline()):
            return pid
    except (OSError, ValueError, psutil.Error):
        pass
    return None


def _kill_tree(pid: int) -> None:
    import psutil
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass


def _fmt_age(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


# ---------------------------------------------------------------------------- commands

def cmd_setup(args) -> None:
    current = read_env()
    print(f"{BOLD}Iriis setup{RESET}: values are saved to {ENV_PATH} (git-ignored). Press Enter to keep the value in [brackets].\n")

    def ask(key: str, prompt: str, secret: bool = False, generate: bool = False) -> str:
        have = current.get(key, "")
        shown = (have[:4] + "…" if secret and have else have) or ("generate one" if generate else "")
        value = input(f"{prompt} [{shown}]: ").strip()
        if not value and not have and generate:
            value = secrets.token_urlsafe(32)
            print(f"  {DIM}generated{RESET}")
        return value or have

    url = ask("IRIIS_SERVER_URL", "Railway URL of her brain (e.g. https://iriis-production.up.railway.app)")
    api = ask("IRIIS_API_KEY", "IRIIS_API_KEY (clients' key)", secret=True, generate=True)
    desk = ask("IRIIS_DESKTOP_TOKEN", "IRIIS_DESKTOP_TOKEN (desktop link secret)", secret=True, generate=True)
    values = {"IRIIS_SERVER_URL": to_http(url) if url else "", "IRIIS_API_KEY": api, "IRIIS_DESKTOP_TOKEN": desk}
    write_env({k: v for k, v in values.items() if v})
    print(f"\n{GREEN}Saved.{RESET} Set the SAME IRIIS_API_KEY and IRIIS_DESKTOP_TOKEN as Variables on Railway"
          f" (plus DEEPSEEK_API_KEY there). Then: iriis start && iriis status")


def cmd_start(args) -> None:
    pid = _running_pid()
    if pid:
        iriis_says(f"I'm already running (pid {pid}). Use `iriis restart` to restart me.")
        return
    _server_url()  # fail early if not configured
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "iriis.desktop", "--background"]
    if not args.no_voice:
        argv.append("--voice")
    if args.push_to_talk:
        argv.append("--push-to-talk")
    if args.quiet:
        argv.append("--quiet")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(ROOT), "COLUMNS": "120"}
    log = open(LOG_FILE, "a", encoding="utf-8")
    log.write(f"\n===== iriis start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log.flush()
    kwargs: dict = {"cwd": str(ROOT), "stdout": log, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(argv, **kwargs)
    PID_FILE.write_text(str(proc.pid))
    print(f"{DIM}started in the background (pid {proc.pid}); log: {LOG_FILE}{RESET}")
    # Wait briefly for the first connection attempt so we can report it.
    deadline = time.time() + args.wait
    while time.time() < deadline:
        time.sleep(1)
        if proc.poll() is not None:
            print(f"{RED}It exited straight away.{RESET} Last log lines:")
            cmd_logs(argparse.Namespace(follow=False, lines=15))
            sys.exit(1)
        st = _read_status()
        if st.get("last_error") not in (None, "starting"):
            break
    _print_status(args)


def cmd_stop(args) -> None:
    pid = _running_pid()
    if not pid:
        print("Iriis isn't running.")
        return
    _kill_tree(pid)
    PID_FILE.unlink(missing_ok=True)
    print(f"Stopped (pid {pid}).")


def cmd_restart(args) -> None:
    cmd_stop(args)
    time.sleep(1)
    cmd_start(args)


def _read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _check_brain() -> tuple[bool, str, dict]:
    """(reachable_and_authorised, human summary, status json)."""
    import httpx
    url = _server_url()
    try:
        r = httpx.get(url + "/health", timeout=8)
        if r.status_code != 200:
            return False, f"{url} answered HTTP {r.status_code}", {}
    except Exception as e:  # noqa: BLE001
        return False, f"can't reach {url} ({e.__class__.__name__})", {}
    try:
        r = httpx.get(url + "/v1/status", headers=_auth(), timeout=10)
    except Exception as e:  # noqa: BLE001
        return False, f"status check failed ({e.__class__.__name__})", {}
    if r.status_code == 401:
        return False, "the brain rejected IRIIS_API_KEY (run `iriis setup`; it must match Railway)", {}
    if r.status_code != 200:
        return False, f"status returned HTTP {r.status_code}", {}
    return True, url, r.json()


def _print_status(args) -> tuple[bool, str]:
    pid = _running_pid()
    st = _read_status() if pid else {}
    ok, summary, info = _check_brain()
    print(f"{BOLD}Iriis status{RESET}")
    if pid:
        import psutil
        age = _fmt_age(time.time() - psutil.Process(pid).create_time())
        print(f"  Desktop agent : {GREEN}running{RESET} (pid {pid}, up {age})")
    else:
        print(f"  Desktop agent : {YELLOW}not running{RESET}  (start with: iriis start)")
    if ok:
        problems = info.get("problems") or []
        print(f"  Brain         : {GREEN}online{RESET} at {summary}")
        print(f"                  LLM {info['llm']['model']} · STT {info.get('stt')} · TTS {info.get('tts')} · {info.get('tools')} tools")
        print(f"                  storage {info.get('storage', '?')}")
        for p in problems:
            print(f"                  {YELLOW}! {p}{RESET}")
    else:
        print(f"  Brain         : {RED}unreachable{RESET} ({summary})")
    linked = bool(info.get("desktop_connected")) if ok else False
    if pid:
        since = st.get("since")
        when = f" for {_fmt_age(time.time() - since)}" if since else ""
        if linked:
            print(f"  Desktop link  : {GREEN}connected{RESET}{when} ({st.get('tools', '?')} local tools)")
        else:
            print(f"  Desktop link  : {RED}not connected{RESET}{when}" + (f" ({st.get('last_error')})" if st.get("last_error") else ""))
        if "voice_connected" in st:
            v = st["voice_connected"]
            print(f"  Voice         : {GREEN + 'connected' if v else RED + 'not connected'}{RESET}")
    # What she says about it:
    if ok and linked:
        line = "I'm connected to my brain and ready."
    elif ok and pid:
        line = "My brain is online, but this computer isn't linked to it yet. I'm still trying."
    elif ok:
        line = "My brain is online, but I'm not running on this computer. Say `iriis start`."
    else:
        line = f"I'm not connected to my brain right now: {summary}."
    iriis_says(line)
    if getattr(args, "say", False):
        _speak(line, online=ok)
    return ok and (linked or not pid), line


def cmd_status(args) -> None:
    good, _ = _print_status(args)
    sys.exit(0 if good else 1)


def _play_wav(data: bytes) -> None:
    import sounddevice as sd
    import soundfile as sf
    audio, sr = sf.read(io.BytesIO(data), dtype="float32")
    sd.play(audio, sr)
    sd.wait()


def _chunks(text: str, max_chars: int = 220) -> list[str]:
    """Split long text into sentence-sized pieces (so speech starts fast and nothing is cut off)."""
    from iriis.llm.sentences import SentenceChunker
    c = SentenceChunker(first_chunk_chars=10_000)
    pieces = c.push(text.replace("\r", "") + "\n\n")
    tail = c.flush()
    if tail:
        pieces.append(tail)
    out: list[str] = []
    for p in (x.strip() for x in pieces if x.strip()):
        while len(p) > max_chars:   # very long sentence: break at a comma or space
            cut = max(p.rfind(", ", 0, max_chars), p.rfind(" ", 0, max_chars))
            cut = cut if cut > 40 else max_chars
            out.append(p[:cut + 1].strip())
            p = p[cut + 1:].strip()
        if p:
            out.append(p)
    return out


def _speak(text: str, online: bool = True) -> None:
    """Say ALL of the text: sentence by sentence, fetching the next one while the current one plays."""
    import concurrent.futures as cf

    import httpx
    from iriis.tts.text_normalize import Delivery
    delivery = Delivery()   # a {voice} or [emotion] carries on across the pieces, as in her live replies
    pieces = []
    for piece in _chunks(text):
        spoken, voice = delivery.apply(piece)
        pieces.append((f"{{{voice}}} " if voice else "") + spoken)
    if not pieces:
        return
    if online:
        client = httpx.Client(timeout=60)

        def fetch(piece: str) -> bytes | None:
            r = client.post(_server_url() + "/v1/tts", json={"text": piece}, headers=_auth())
            return r.content if r.status_code == 200 else None

        try:
            with cf.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(fetch, pieces[0])]
                for i in range(len(pieces)):
                    if i + 1 < len(pieces):
                        futures.append(pool.submit(fetch, pieces[i + 1]))
                    wav = futures[i].result()
                    if wav is None:
                        raise RuntimeError("tts failed")
                    _play_wav(wav)
            return
        except KeyboardInterrupt:
            return
        except Exception:  # noqa: BLE001  (fall back to the on-device voice)
            pass
    # Offline: on-device voice.
    from iriis.config import load_config
    from iriis.tts import make_tts
    from iriis.tts.text_normalize import normalize
    import sounddevice as sd
    cfg = load_config()
    for backend in ("kokoro", "piper"):   # on-device voices only (offline)
        try:
            tts = make_tts(cfg.tts, backend)
            for piece in pieces:
                sd.play(tts.synthesize(normalize(piece)), tts.sample_rate)
                sd.wait()
            return
        except KeyboardInterrupt:
            return
        except Exception:  # noqa: BLE001
            continue


def _read_text(args) -> str:
    """Text from the arguments, a file (-f), or stdin (`-`, or a pipe / heredoc)."""
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
    words = list(args.text or [])
    if words == ["-"] or (not words and not sys.stdin.isatty()):
        return sys.stdin.read()
    if not words:
        sys.exit("Give me some text, a file (-f notes.txt), or pipe it in:  iriis say <<'EOF' ... EOF")
    return " ".join(words)


def cmd_say(args) -> None:
    text = _read_text(args)
    ok, _, _ = _check_brain()
    _speak(text, online=ok)


def cmd_ask(args) -> None:
    import httpx
    ok, summary, _ = _check_brain()
    if not ok:
        iriis_says(f"I'm not connected to my brain right now ({summary}).")
        sys.exit(1)
    url = _server_url()
    body = {"message": _read_text(args), "session_id": args.session, "stream": True, "speak": args.speak}
    clips: list[bytes] = []
    printed_name = False
    with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
        with client.stream("POST", url + "/v1/chat", json=body, headers=_auth()) as resp:
            if resp.status_code != 200:
                sys.exit(f"{RED}HTTP {resp.status_code}{RESET}: {resp.read()[:300]!r}")
            event = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                if event == "sentence":
                    if not printed_name:
                        print(f"{PURPLE}{BOLD}Iriis:{RESET} ", end="")
                        printed_name = True
                    print(ev["text"], end=" ", flush=True)
                elif event == "tool" and args.verbose:
                    print(f"\n  {DIM}{ev['text']}{RESET}")
                elif event == "screen":
                    print(f"\n  {BOLD}On screen:{RESET} {ev['text']}")
                elif event == "audio":
                    clips.append(base64.b64decode(ev["data"]))
                elif event == "confirm":
                    print(f"\n{YELLOW}{ev['question']}{RESET}")
                    reply = input("yes/no> ")
                    client.post(url + "/v1/confirm", headers=_auth(),
                                json={"session_id": ev["session_id"], "id": ev["id"], "text": reply})
                elif event == "error":
                    print(f"\n{RED}{ev.get('message')}{RESET}")
                elif event == "done":
                    print()
                    if args.verbose and ev.get("timings"):
                        print(f"{DIM}{ev['timings']}{RESET}")
    for clip in clips:
        _play_wav(clip)


def cmd_logs(args) -> None:
    if not LOG_FILE.exists():
        print("No log yet.")
        return
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    print("\n".join(lines[-args.lines:]))
    if args.follow:
        with LOG_FILE.open(encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            try:
                while True:
                    line = f.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(0.5)
            except KeyboardInterrupt:
                pass


OPEN_SAMPLE_LINES = {
    "tara": "Hi, I'm Tara, Iriis' own voice. <laugh> I'll be telling you the story, and it's a good one!",
    "leah": "Hi, I'm Leah. Sit down, the meeting starts now. <sigh> Well done, my dear. Really.",
    "jess": "Hi, I'm Jess! Oh my gosh, did you see that? <laugh> Don't tell anyone.",
    "mia": "Hi, I'm Mia. Um, I'm a bit nervous... <gasp> wait, we won? We actually won!",
    "zoe": "Hi, I'm Zoe. It's okay. You did your best, and that's enough for today.",
    "leo": "Hi, I'm Leo. Nobody leaves this room. <chuckle> Relax, I'm joking.",
    "dan": "Hi, I'm Dan. Bro, you won't believe what happened today. <laugh> Classic.",
    "zac": "Hi, I'm Zac! Let's go, let's go, let's go! <groan> Okay, fine, I'll calm down.",
}

SAMPLE_LINES = {
    "hannah": "Hi, I'm Hannah, Iriis' own voice. [warm] I'll be telling you the story. [excited] And it's a good one!",
    "diana": "Hi, I'm Diana. [serious] Sit down, the meeting starts now. [warm] Well done, my dear.",
    "autumn": "Hi, I'm Autumn! [excited] Oh my gosh, did you see that? [whisper] Don't tell anyone.",
    "troy": "Hi, I'm Troy. [menacing] Nobody leaves this room. [laughing] Relax, I'm joking.",
    "austin": "Hi, I'm Austin. [casual] Bro, you won't believe what happened today. [excited] We won!",
    "daniel": "Hi, I'm Daniel. [calm] The results are in, and they look good. [serious] But we must be careful.",
}


def cmd_voices(args) -> None:
    """Play every voice (neutral, then with a couple of emotions) so you can write the casting sheet."""
    from iriis.config import load_config
    cfg = load_config()
    ok, summary, info = _check_brain()
    if not ok:
        sys.exit(f"I'm not connected to my brain right now ({summary}), so I can't play her voices.")
    if not str(info.get("tts", "")).startswith("orpheus"):
        print(f"{YELLOW}Her brain is using {info.get('tts')} (calm voice), not Orpheus,{RESET} so every sample would sound"
              " the same. Set GROQ_API_KEY on Railway and accept the Orpheus terms first.")
        return
    flavor = "open" if str(info.get("tts", "")).startswith("orpheus-open") else "groq"
    cfg.tts.orpheus_flavor = flavor
    sheet = cfg.tts.casting()
    lines = OPEN_SAMPLE_LINES if flavor == "open" else SAMPLE_LINES
    voices = [args.voice] if args.voice else list(sheet)
    for v in voices:
        print(f"\n{BOLD}{{{v}}}{RESET}  casting sheet says: {DIM}{sheet.get(v, '?')}{RESET}")
        _speak(f"{{{v}}} {lines.get(v, 'Hello, this is my voice.')}", online=True)
    print(f"\nNow describe what you heard in {BOLD}config/config.yaml{RESET} under tts.voice_casting.{flavor}"
          " (e.g. troy: \"male; very deep, slow: villains, kings, old men\"), push, and she'll cast from that.")


def cmd_mic(args) -> None:
    """Live mic check: a level meter plus the wake-word score, so you can see what she hears."""
    import queue

    import numpy as np
    import sounddevice as sd

    from iriis.audio.devices import resolve_device
    from iriis.audio.wakeword import WakeWord
    from iriis.config import load_config

    cfg = load_config()
    if args.list:
        from iriis.audio.devices import print_devices
        print_devices()
        return
    dev = resolve_device(args.device if args.device is not None else cfg.audio.input_device, "input")
    name = sd.query_devices(dev if dev is not None else sd.default.device[0])["name"]
    ww = WakeWord(cfg.wakeword)
    q: queue.Queue = queue.Queue()
    print(f"{BOLD}Mic test{RESET} on: {name}   (wake-word threshold {ww.threshold})")
    print(f"Say {BOLD}\"Hey Jarvis\"{RESET} a few times, and talk normally. The bar should jump when you speak. Ctrl+C to stop.\n")
    loudest, hits = 0.0, 0
    try:
        with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=1280, device=dev,
                            callback=lambda d, f, t, s: q.put(d[:, 0].copy())):
            end = time.time() + args.seconds
            while time.time() < end:
                block = q.get()
                level = float(np.sqrt(np.mean(block ** 2)))
                loudest = max(loudest, level)
                fired = ww.feed(block)
                bar = "#" * min(40, int(level * 400))
                score = ww.last_score
                print(f"\r  level {level:6.3f} |{bar:<40}| wake score {score:4.2f} ", end="", flush=True)
                if fired:
                    hits += 1
                    print(f"\n  {GREEN}{BOLD}WAKE WORD DETECTED{RESET} (score {score:.2f})")
                    ww.reset()
    except KeyboardInterrupt:
        pass
    print("\n")
    if loudest < 0.005:
        print(f"{RED}She heard (almost) nothing.{RESET} Unmute the mic (laptop mic key / Settings > System > Sound > Input),"
              " or try another device: `iriis mic --list`, then `iriis mic --device <number>`.")
    elif hits == 0:
        print(f"{YELLOW}She hears you, but the wake word never fired.{RESET} Say \"Hey Jarvis\" (not \"Iriis\" yet), a bit slower,"
              " or lower wakeword.threshold to 0.35 in config/config.yaml. Or use push-to-talk: `iriis restart --push-to-talk` (hold F9).")
    else:
        print(f"{GREEN}All good:{RESET} the wake word fired {hits} time(s). If she still doesn't respond, run `iriis restart`.")
    if args.device is not None:
        print(f"To make this mic permanent, set  audio.input_device: {args.device}  in config/config.yaml, then `iriis restart`.")


def cmd_server(args) -> None:
    sys.exit(subprocess.call([sys.executable, "-m", "iriis.server"], cwd=str(ROOT)))


def main() -> None:
    load_env()
    p = argparse.ArgumentParser(prog="iriis", description="Control Iriis, your voice assistant.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="save the Railway URL and keys to .env").set_defaults(fn=cmd_setup)
    for name, fn in (("start", cmd_start), ("restart", cmd_restart)):
        sp = sub.add_parser(name, help=f"{name} the desktop agent in the background")
        sp.add_argument("--no-voice", action="store_true", help="tools only: no mic/speaker")
        sp.add_argument("--push-to-talk", action="store_true")
        sp.add_argument("--quiet", action="store_true", help="don't announce connection changes out loud")
        sp.add_argument("--wait", type=float, default=20, help="seconds to wait for the first connection")
        sp.set_defaults(fn=fn)
    sub.add_parser("stop", help="stop the background agent").set_defaults(fn=cmd_stop)
    sp = sub.add_parser("status", help="running? connected to her brain?")
    sp.add_argument("--say", action="store_true", help="also say it out loud")
    sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("ask", help="ask her something")
    sp.add_argument("text", nargs="*", help="your message; or use -f FILE, or pipe/heredoc it in")
    sp.add_argument("-f", "--file", help="read the message from a text file")
    sp.add_argument("--speak", action="store_true", help="play her spoken reply")
    sp.add_argument("--session", default="owner", help="conversation id (default: owner, shared with voice)")
    sp.add_argument("-v", "--verbose", action="store_true", help="show tool calls and timings")
    sp.set_defaults(fn=cmd_ask)
    sp = sub.add_parser("say", help="have her say something, word for word")
    sp.add_argument("text", nargs="*", help="text to say; or use -f FILE, or pipe/heredoc it in")
    sp.add_argument("-f", "--file", help="read the text from a file")
    sp.set_defaults(fn=cmd_say)
    sp = sub.add_parser("logs", help="show the background agent's log")
    sp.add_argument("-f", "--follow", action="store_true")
    sp.add_argument("-n", "--lines", type=int, default=40)
    sp.set_defaults(fn=cmd_logs)
    sp = sub.add_parser("voices", help="hear every voice she can cast (to write the casting sheet)")
    sp.add_argument("voice", nargs="?", help="just one voice, e.g. troy")
    sp.set_defaults(fn=cmd_voices)
    sp = sub.add_parser("mic", help="live mic + wake-word test")
    sp.add_argument("--device", help="input device number or name (see --list)")
    sp.add_argument("--list", action="store_true", help="list audio devices")
    sp.add_argument("--seconds", type=float, default=30)
    sp.set_defaults(fn=cmd_mic)
    sub.add_parser("server", help="run the cloud core locally (development)").set_defaults(fn=cmd_server)
    args = p.parse_args()
    try:
        args.fn(args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
