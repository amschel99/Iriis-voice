"""Convert AfriSpeech fine-tuned Whisper models (Hugging Face) to CTranslate2 for faster-whisper.

Needs `transformers` + `torch` at conversion time only. Keep them out of the runtime venv:

    python -m venv .venv-convert
    .venv-convert/Scripts/pip install -r scripts/requirements-convert.txt      (Linux/macOS: .venv-convert/bin/pip)
    .venv-convert/Scripts/python scripts/convert_models.py                     # default set
    .venv-convert/Scripts/python scripts/convert_models.py --models professor-small --quantization float16

Equivalent CLI for one model:
    ct2-transformers-converter --model intronhealth/afrispeech-whisper-medium-all \
        --output_dir models/stt/afrispeech-whisper-medium-ct2 --quantization int8 \
        --copy_files tokenizer.json preprocessor_config.json
(that repo has no tokenizer.json; this script generates one so faster-whisper never goes online.)
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "models" / "stt"

MODELS = {
    # key: (HF repo, output dir, licence)
    "intron-medium": ("intronhealth/afrispeech-whisper-medium-all", "afrispeech-whisper-medium-ct2", "Apache-2.0"),
    "professor-small": ("Professor/whisper-small-afrispeech", "whisper-small-afrispeech-ct2", "CC-BY-NC-SA-4.0 (non-commercial)"),
    "professor-medium": ("Professor/whisper-medium-afrispeech", "whisper-medium-afrispeech-ct2", "CC-BY-NC-SA-4.0 (non-commercial)"),
}
# Stock (non-accent-tuned) baseline, already in CT2 format; just downloaded.
BASELINES = {"small.en": ("Systran/faster-whisper-small.en", "faster-whisper-small.en", "MIT")}


def convert(repo: str, out_dir: Path, quantization: str) -> None:
    from huggingface_hub import snapshot_download
    from transformers import WhisperFeatureExtractor, WhisperTokenizerFast
    import ctranslate2

    if (out_dir / "model.bin").exists():
        print(f"  ok   {out_dir.relative_to(ROOT)} (already converted)")
        return
    print(f"  download {repo}")
    local = Path(snapshot_download(repo, allow_patterns=["*.json", "*.safetensors", "*.bin", "*.txt", "*.model"],
                                   ignore_patterns=["optimizer*", "training_args*", "*.msgpack", "*.h5", "*.ot"]))
    work = OUT / ("_work_" + out_dir.name)
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(local, work)
    if not (work / "tokenizer.json").exists():
        print("  generating tokenizer.json")
        WhisperTokenizerFast.from_pretrained(work).save_pretrained(work)
    if not (work / "preprocessor_config.json").exists():
        n_mels = json.loads((work / "config.json").read_text()).get("num_mel_bins", 80)
        WhisperFeatureExtractor(feature_size=n_mels).save_pretrained(work)
    print(f"  convert -> {out_dir.relative_to(ROOT)} ({quantization})")
    ctranslate2.converters.TransformersConverter(
        str(work), copy_files=["tokenizer.json", "preprocessor_config.json"], load_as_float16=quantization.startswith("float16"),
    ).convert(str(out_dir), quantization=quantization, force=True)
    shutil.rmtree(work, ignore_errors=True)
    _ensure_suppress_tokens(out_dir)


def _ensure_suppress_tokens(out_dir: Path) -> None:
    """Fine-tunes often ship without generation_config.json, which leaves the CT2 model with no
    suppressed tokens (worse transcripts). Copy the list from the matching base OpenAI model."""
    from transformers import GenerationConfig

    cfg_path = out_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    if cfg.get("suppress_ids"):
        return
    n_layers = len({h[0] for h in cfg.get("alignment_heads", [])}) or 0
    vocab = json.loads((out_dir / "vocabulary.json").read_text()) if (out_dir / "vocabulary.json").exists() else []
    size = "large-v3" if len(vocab) > 51865 else "medium" if n_layers >= 12 else "small"
    base = GenerationConfig.from_pretrained(f"openai/whisper-{size}")
    cfg["suppress_ids"], cfg["suppress_ids_begin"] = base.suppress_tokens, base.begin_suppress_tokens
    cfg_path.write_text(json.dumps(cfg))
    print(f"  patched suppress tokens from openai/whisper-{size}")


def download_baseline(repo: str, out_dir: Path) -> None:
    from huggingface_hub import snapshot_download
    if (out_dir / "model.bin").exists():
        print(f"  ok   {out_dir.relative_to(ROOT)}")
        return
    print(f"  download {repo}")
    snapshot_download(repo, local_dir=out_dir)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="*", default=["intron-medium", "professor-small", "small.en"],
                   choices=[*MODELS, *BASELINES])
    p.add_argument("--quantization", default="int8", help="int8 (CPU), float16 (GPU) or int8_float16")
    p.add_argument("--output-name", help="folder name under models/stt (only with a single --models entry)")
    args = p.parse_args()
    if args.output_name and len(args.models) != 1:
        p.error("--output-name needs exactly one --models entry")
    OUT.mkdir(parents=True, exist_ok=True)
    for key in args.models:
        if key in BASELINES:
            repo, name, lic = BASELINES[key]
            print(f"[{key}] {repo}  licence: {lic}")
            download_baseline(repo, OUT / (args.output_name or name))
        else:
            repo, name, lic = MODELS[key]
            print(f"[{key}] {repo}  licence: {lic}")
            convert(repo, OUT / (args.output_name or name), args.quantization)
    print("Done. Point stt.model in config/config.yaml at one of the folders in models/stt/.")


if __name__ == "__main__":
    main()
