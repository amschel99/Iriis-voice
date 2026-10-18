"""python -m iriis [--text] [--list-devices] [--config PATH] [--dry-run] [--once "question"]"""
from __future__ import annotations

import argparse
import asyncio

from iriis.config import apply_offline_env, load_config

apply_offline_env()  # before any model library is imported


def main() -> None:
    p = argparse.ArgumentParser(prog="iriis", description="Iriis all-in-one local app (use --offline for no network; see iriis.server and iriis.desktop for cloud mode)")
    p.add_argument("--text", action="store_true", help="type instead of talk")
    p.add_argument("--speak", action="store_true", help="in text mode, also speak replies with TTS")
    p.add_argument("--list-devices", action="store_true", help="list audio devices and exit")
    p.add_argument("--config", help="path to config.yaml")
    p.add_argument("--dry-run", action="store_true", help="log tool calls without executing them")
    p.add_argument("--no-tools", action="store_true", help="don't start MCP servers")
    p.add_argument("--model", help="override the LLM model")
    p.add_argument("--offline", action="store_true", help="fully local: Ollama LLM, no network needed")
    p.add_argument("--once", metavar="TEXT", help="answer one question in text mode and exit")
    p.add_argument("--push-to-talk", action="store_true", help="use the push-to-talk key instead of the wake word")
    args = p.parse_args()

    if args.list_devices:
        from iriis.audio.devices import print_devices
        print_devices()
        return

    cfg = load_config(args.config)
    if args.dry_run:
        cfg.permissions.dry_run = True
    if args.offline:
        cfg.llm.backend = "ollama"
        cfg.llm.model = cfg.llm.offline_model
        cfg.llm.fallback_model = ""
    if args.model:
        cfg.llm.model = args.model
        cfg.llm.fallback_model = ""
    if args.push_to_talk:
        cfg.wakeword.mode = "push_to_talk"

    from iriis.app import Iriis, configure_logging
    configure_logging()
    app = Iriis(cfg, use_tools=not args.no_tools)
    try:
        if args.text or args.once:
            asyncio.run(app.run_text(once=args.once, speak=args.speak))
        else:
            from iriis.voice import run_voice
            asyncio.run(run_voice(app))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
