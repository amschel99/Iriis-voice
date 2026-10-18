"""Record your own accent benchmark: reads each prompt aloud-style on screen, you say it, it saves a WAV.

    python scripts/record_benchmark.py                       # 30 built-in prompts -> benchmarks/my_voice/
    python scripts/record_benchmark.py --prompts my_prompts.txt --out benchmarks/my_voice

Recording stops automatically after ~1 s of silence (Silero VAD). Re-run to redo missing files only.
Writes transcripts.txt as `file.wav|exact words you were asked to say`.
"""
from __future__ import annotations

import argparse
import queue
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROMPTS = [
    "Hey, what time is it in London right now?",
    "Remind me to call Wanjiru at five thirty pm.",
    "Set a timer for ten minutes.",
    "How much battery do I have left?",
    "Open Chrome and go to the Nation website.",
    "What's fifteen percent of two thousand four hundred shillings?",
    "Search my documents for the Safaricom invoice.",
    "Turn the volume down a little.",
    "Take a note: buy sukuma wiki and unga on the way home.",
    "What's on my clipboard?",
    "Remember that my gym is at Sarit Centre in Westlands.",
    "Where is my gym?",
    "How much disk space is free on this laptop?",
    "Convert twenty five kilometres to miles.",
    "Which programs are using the most memory?",
    "Play the next song.",
    "Lock the screen.",
    "Read me the file called project notes.",
    "List the reminders I have for today.",
    "Run git status in my projects folder.",
    "What's the date next Friday?",
    "How long has the computer been on?",
    "Is the internet working?",
    "Cancel that, never mind.",
    "Move the report from Downloads to Documents.",
    "Take a screenshot.",
    "What's three hundred and forty divided by eight?",
    "Tell Otieno I'll be late for the meeting in Kilimani.",
    "Start over, new conversation.",
    "What is the time difference between Nairobi and New York?",
]


def record_one(sr: int = 16000) -> np.ndarray:
    import sounddevice as sd
    from iriis.audio.vad import Endpointer, SileroVAD

    ep = Endpointer(SileroVAD(), silence_ms=1000, no_speech_timeout_s=10, max_utterance_s=15)
    q: queue.Queue[np.ndarray] = queue.Queue()
    with sd.InputStream(samplerate=sr, channels=1, dtype="float32", blocksize=1280,
                        callback=lambda d, f, t, s: q.put(d[:, 0].copy())):
        while True:
            ev = ep.feed(q.get())
            if ev and ev.kind == "timeout":
                return np.zeros(0, dtype=np.float32)
            if ev and ev.kind == "speech_end":
                return ev.audio if ev.audio is not None else np.zeros(0, dtype=np.float32)


def main() -> None:
    import soundfile as sf

    p = argparse.ArgumentParser()
    p.add_argument("--prompts", help="text file, one prompt per line")
    p.add_argument("--out", default=str(ROOT / "benchmarks" / "my_voice"))
    args = p.parse_args()
    prompts = PROMPTS if not args.prompts else [l.strip() for l in Path(args.prompts).read_text(encoding="utf-8").splitlines() if l.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    print("Speak naturally, the way you'd talk to Iriis. Press Enter before each prompt; Ctrl+C to stop.\n")
    for i, prompt in enumerate(prompts, 1):
        name = f"cmd_{i:02d}.wav"
        lines.append(f"{name}|{prompt}")
        if (out / name).exists():
            continue
        input(f"[{i}/{len(prompts)}] Press Enter, then say:\n    \"{prompt}\"")
        while True:
            audio = record_one()
            if len(audio) > 16000 * 0.4:
                break
            print("    didn't hear anything; try again")
        sf.write(out / name, audio, 16000)
        print(f"    saved {name} ({len(audio) / 16000:.1f} s)")
        (out / "transcripts.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "transcripts.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nDone. Now run: python scripts/benchmark_stt.py --audio-dir {out}")


if __name__ == "__main__":
    main()
