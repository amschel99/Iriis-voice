"""Desktop side of cloud Iriis.

    python -m iriis.desktop --server wss://your-app.up.railway.app            # tools only
    python -m iriis.desktop --server wss://your-app.up.railway.app --voice    # + wake word, mic, speaker
    python -m iriis.desktop --confirm local                                   # also ask here before risky actions

Usually started with the CLI instead: `iriis start` (see iriis/cli.py).
Settings come from the environment or .env: IRIIS_SERVER_URL, IRIIS_DESKTOP_TOKEN (tools link), IRIIS_API_KEY (voice).
"""
from __future__ import annotations

import argparse
import asyncio

from iriis.config import apply_offline_env, load_config

apply_offline_env()


async def _main(args) -> None:
    import os

    from iriis.app import configure_logging
    from iriis.desktop.agent import STATUS_FILE, DesktopAgent
    from iriis.envfile import load_env
    from iriis.ui.console import ConsoleUI

    load_env()                      # .env written by `iriis setup`
    configure_logging()
    cfg = load_config(args.config)
    cfg.desktop.server_url = args.server or os.environ.get("IRIIS_SERVER_URL") or cfg.desktop.server_url
    STATUS_FILE.unlink(missing_ok=True)
    if args.confirm:
        cfg.desktop.confirm_mode = args.confirm
    if args.push_to_talk:
        cfg.wakeword.mode = "push_to_talk"
    if args.dry_run:
        cfg.permissions.dry_run = True
    if args.background and cfg.desktop.confirm_mode == "local":
        cfg.desktop.confirm_mode = "cloud"   # no terminal to ask in when running in the background
    ui = ConsoleUI(cfg.ui.show_latency)
    agent = DesktopAgent(cfg, ui)
    player = local_voice = None
    if args.voice:
        from iriis.audio.playback import Player
        from iriis.desktop.local_voice import LocalVoice
        player = Player(cfg.audio.output_device, 24000)
        local_voice = LocalVoice(cfg, player)
        if not args.quiet:
            agent.say_locally = local_voice.say   # "I'm connected / I'm not connected"
    await agent.start_tools()
    tasks = [asyncio.create_task(agent.run(), name="desktop-link")]
    if args.voice:
        from iriis.desktop.voice_client import RemoteVoiceClient
        tasks.append(asyncio.create_task(RemoteVoiceClient(cfg, ui, player, local_voice).run(), name="voice"))
    try:
        await asyncio.gather(*tasks)
    finally:
        await agent.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="iriis.desktop")
    p.add_argument("--server", help="cloud URL, e.g. wss://your-app.up.railway.app")
    p.add_argument("--voice", action="store_true", help="also run the wake word + mic + speaker client")
    p.add_argument("--confirm", choices=["cloud", "local"], help="where risky actions are confirmed")
    p.add_argument("--push-to-talk", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--quiet", action="store_true", help="don't announce connection changes out loud")
    p.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--config")
    try:
        asyncio.run(_main(p.parse_args()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
