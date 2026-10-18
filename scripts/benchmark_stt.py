"""Accent benchmark: word error rate (WER) and real-time factor (RTF) for every STT backend/model.

    python scripts/benchmark_stt.py --audio-dir benchmarks/my_voice
    python scripts/benchmark_stt.py --audio-dir benchmarks/my_voice --write-config   # adopt the winner
    python scripts/benchmark_stt.py --afrispeech 40 --accents swahili kikuyu luo     # optional, needs HF token

--audio-dir must contain WAVs plus transcripts.txt with lines `file.wav|reference text`
(a tab works as the separator too). Record your own with scripts/record_benchmark.py.

AfriSpeech-200 is gated (accept the terms on huggingface.co, then `huggingface-cli login`), is
licensed CC-BY-NC-SA-4.0, and needs `pip install "datasets<4" librosa` because it uses a loading
script. That is a one-off online step; the benchmark itself runs offline.

The default is chosen as: lowest WER among engines whose mean latency on your clips is within
--latency-budget seconds (default 0.8 s, leaving room for the LLM and TTS in the 1.5 s target).
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from iriis.config import load_config  # noqa: E402


def normalise(text: str) -> str:
    from iriis.tts.text_normalize import normalize as spoken
    text = spoken(text)  # digits -> words so "5:30" and "five thirty" compare equal
    text = text.lower().replace("-", " ")
    text = re.sub(r"[^a-z' ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_folder(folder: Path) -> list[tuple[str, np.ndarray, str]]:
    import soundfile as sf

    tfile = folder / "transcripts.txt"
    if not tfile.exists():
        raise SystemExit(f"{tfile} not found (format: file.wav|reference text)")
    items = []
    for line in tfile.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, _, ref = line.partition("|") if "|" in line else line.partition("\t")
        path = folder / name.strip()
        if not path.exists():
            print(f"  skip missing {path.name}")
            continue
        audio, sr = sf.read(path, dtype="float32", always_2d=True)
        audio = audio.mean(axis=1)
        if sr != 16000:
            from iriis.audio.playback import resample
            audio = resample(audio, sr, 16000)
        items.append((path.name, audio, ref.strip()))
    return items


def load_afrispeech(n: int, accents: list[str]) -> list[tuple[str, np.ndarray, str]]:
    os.environ["HF_HUB_OFFLINE"] = "0"
    try:
        from datasets import load_dataset
    except ImportError:
        raise SystemExit('AfriSpeech needs: pip install "datasets<4" librosa  (and huggingface-cli login)')
    items = []
    per = max(1, n // len(accents))
    for accent in accents:
        ds = load_dataset("intronhealth/afrispeech-200", accent, split="test", streaming=True, trust_remote_code=True)
        for i, row in enumerate(ds):
            if i >= per:
                break
            a = row["audio"]
            audio = np.asarray(a["array"], dtype=np.float32)
            if a["sampling_rate"] != 16000:
                from iriis.audio.playback import resample
                audio = resample(audio, a["sampling_rate"], 16000)
            items.append((f"{accent}/{row.get('audio_id', i)}", audio, row["transcript"]))
    os.environ["HF_HUB_OFFLINE"] = "1"
    return items


def discover_engines(cfg) -> list[tuple[str, str]]:
    stt_dir = ROOT / "models" / "stt"
    engines = []
    for d in sorted(stt_dir.glob("*")) if stt_dir.exists() else []:
        if (d / "model.bin").exists():
            engines.append(("faster_whisper", str(d)))
        elif any(d.glob("encoder*.onnx")):
            engines.append(("parakeet", str(d)))
    return engines


def rss_mb() -> float:
    import psutil
    return psutil.Process().memory_info().rss / 1e6


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--audio-dir", help="folder with WAVs + transcripts.txt")
    p.add_argument("--afrispeech", type=int, default=0, help="also evaluate N AfriSpeech-200 test clips")
    p.add_argument("--accents", nargs="*", default=["swahili", "kikuyu", "luo", "luhya"])
    p.add_argument("--engines", nargs="*", help="backend:path entries; default = everything in models/stt")
    p.add_argument("--latency-budget", type=float, default=0.8, help="max mean seconds per clip")
    p.add_argument("--write-config", action="store_true", help="set stt.backend/model in config.yaml to the winner")
    p.add_argument("--show", action="store_true", help="print every hypothesis")
    args = p.parse_args()

    import jiwer
    from iriis.stt import make_stt

    cfg = load_config()
    data = []
    if args.audio_dir:
        data += load_folder(Path(args.audio_dir))
    if args.afrispeech:
        data += load_afrispeech(args.afrispeech, args.accents)
    if not data:
        raise SystemExit("No audio. Pass --audio-dir (record with scripts/record_benchmark.py) and/or --afrispeech N.")
    engines = [tuple(e.split(":", 1)) for e in args.engines] if args.engines else discover_engines(cfg)
    if not engines:
        raise SystemExit("No STT models found in models/stt. Run download_models.py / convert_models.py.")
    total_audio = sum(len(a) for _, a, _ in data) / 16000
    print(f"{len(data)} clips, {total_audio:.0f} s of audio, {len(engines)} engines\n")

    rows = []
    for backend, path in engines:
        label = f"{backend}:{Path(path).name}"
        before = rss_mb()
        t0 = time.perf_counter()
        try:
            engine = make_stt(cfg.stt, backend, path)
            engine.warmup()
        except Exception as e:  # noqa: BLE001
            print(f"  {label}: failed to load ({e})")
            continue
        load_s = time.perf_counter() - t0
        refs, hyps, lat = [], [], []
        for name, audio, ref in data:
            tr = engine.transcribe(audio)
            lat.append(tr.elapsed_s)
            refs.append(normalise(ref))
            hyps.append(normalise(tr.text) or "<empty>")
            if args.show:
                print(f"    {name}: {tr.text!r}")
        wer = jiwer.wer(refs, hyps)
        mem = rss_mb() - before
        rtf = sum(lat) / total_audio
        rows.append({"label": label, "backend": backend, "path": path, "wer": wer, "rtf": rtf,
                     "mean_s": statistics.mean(lat), "p90_s": sorted(lat)[int(0.9 * (len(lat) - 1))],
                     "load_s": load_s, "mem_mb": mem})
        print(f"  {label}: WER {wer:.1%}  RTF {rtf:.3f}  mean {statistics.mean(lat):.2f}s  (+{mem:.0f} MB RAM)")
        del engine

    if not rows:
        return
    from rich.console import Console
    from rich.table import Table
    t = Table(title="STT benchmark (lower is better)")
    for c in ("engine", "WER", "RTF", "mean s/clip", "p90 s", "load s", "RAM MB"):
        t.add_column(c, justify="right" if c != "engine" else "left")
    for r in sorted(rows, key=lambda r: r["wer"]):
        t.add_row(r["label"], f"{r['wer']:.1%}", f"{r['rtf']:.3f}", f"{r['mean_s']:.2f}", f"{r['p90_s']:.2f}", f"{r['load_s']:.1f}", f"{r['mem_mb']:.0f}")
    Console().print(t)

    ok = [r for r in rows if r["mean_s"] <= args.latency_budget] or rows
    best = min(ok, key=lambda r: (r["wer"], r["mean_s"]))
    print(f"\nRecommended default: {best['label']} (WER {best['wer']:.1%}, {best['mean_s']:.2f} s/clip)")
    if args.write_config:
        import yaml
        cpath = ROOT / "config" / "config.yaml"
        text = cpath.read_text(encoding="utf-8")
        rel = os.path.relpath(best["path"], ROOT).replace("\\", "/")
        text = re.sub(r"(?m)^(\s*backend:\s*)(faster_whisper|parakeet)(.*)$", lambda m: m.group(1) + best["backend"] + m.group(3), text, count=1)
        key = "model" if best["backend"] == "faster_whisper" else "parakeet_dir"
        text = re.sub(rf"(?m)^(  {key}:\s*)(\S+)", lambda m: m.group(1) + rel, text, count=1)
        yaml.safe_load(text)  # still valid
        cpath.write_text(text, encoding="utf-8")
        print(f"config.yaml updated: stt.backend={best['backend']}, stt.{key}={rel}")


if __name__ == "__main__":
    main()
