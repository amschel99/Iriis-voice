"""Download every model Iriis needs, once, into ./models (and pull the Ollama models).

    python scripts/download_models.py            # everything
    python scripts/download_models.py --only tts wakeword
    python scripts/download_models.py --skip-ollama

After this, Iriis runs fully offline. The Whisper AfriSpeech models need a conversion
step instead (scripts/convert_models.py), because no CTranslate2 build is published.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
KOKORO = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/"

FILES: dict[str, list[tuple[str, str]]] = {
    "wakeword": [
        (OWW + "hey_jarvis_v0.1.onnx", "wakeword/hey_jarvis_v0.1.onnx"),
        (OWW + "melspectrogram.onnx", "wakeword/melspectrogram.onnx"),
        (OWW + "embedding_model.onnx", "wakeword/embedding_model.onnx"),
    ],
    # Silero VAD: the v6 ONNX bundled inside the faster-whisper package is used; nothing to fetch.
    "tts": [
        (KOKORO + "kokoro-v1.0.int8.onnx", "tts/kokoro-v1.0.int8.onnx"),
        (KOKORO + "voices-v1.0.bin", "tts/voices-v1.0.bin"),
        (PIPER + "en_GB/alba/medium/en_GB-alba-medium.onnx", "tts/en_GB-alba-medium.onnx"),
        (PIPER + "en_GB/alba/medium/en_GB-alba-medium.onnx.json", "tts/en_GB-alba-medium.onnx.json"),
    ],
    "parakeet": [
        (SHERPA + "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2", "stt/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2"),
    ],
}
OLLAMA_MODELS = ["qwen3:8b", "qwen3:4b-instruct-2507-q4_K_M"]


def fetch(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  ok   {dest.relative_to(ROOT)}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  get  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "iriis-model-downloader"})
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r       {done / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    if total and done != total:
        tmp.unlink(missing_ok=True)
        raise IOError(f"incomplete download of {url} ({done} of {total} bytes); re-run the script")
    tmp.replace(dest)


def extract(archive: Path) -> None:
    target = archive.parent / archive.name.replace(".tar.bz2", "")
    if target.exists():
        return
    print(f"  untar {archive.name}")
    with tarfile.open(archive) as tf:
        tf.extractall(archive.parent, filter="data")


def ollama_pull(models: list[str]) -> None:
    exe = shutil.which("ollama")
    if not exe:
        print("  ! ollama not found on PATH; install it from https://ollama.com and run: ollama pull " + " ".join(models))
        return
    for m in models:
        print(f"  ollama pull {m}")
        subprocess.run([exe, "pull", m], check=False)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", nargs="*", choices=[*FILES, "ollama"], help="subset to download")
    p.add_argument("--skip-ollama", action="store_true")
    args = p.parse_args()
    groups = args.only or [*FILES, "ollama"]
    for g in groups:
        if g == "ollama":
            if not args.skip_ollama:
                print("[ollama]")
                ollama_pull(OLLAMA_MODELS)
            continue
        print(f"[{g}]")
        for url, rel in FILES[g]:
            dest = MODELS / rel
            if rel.endswith(".tar.bz2") and (MODELS / rel.replace(".tar.bz2", "")).exists():
                print(f"  ok   {rel.replace('.tar.bz2', '')}")
                continue
            fetch(url, dest)
            if rel.endswith(".tar.bz2"):
                extract(dest)
                dest.unlink(missing_ok=True)
    print("\nDone. Next: python scripts/convert_models.py  (AfriSpeech Whisper -> CTranslate2)")
    print("Then verify offline loading:  python scripts/verify_offline.py")


if __name__ == "__main__":
    sys.exit(main())
