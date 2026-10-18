"""Audio device listing and selection."""
from __future__ import annotations


def print_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())
    try:
        di, do = sd.default.device
        print(f"\nDefault input: {di}   default output: {do}")
    except Exception:
        pass
    print("Set audio.input_device / audio.output_device in config/config.yaml to an index or a name substring.")


def resolve_device(spec: int | str | None, kind: str) -> int | None:
    """Accept None (system default), an index, or a case-insensitive name substring."""
    if spec is None or isinstance(spec, int):
        return spec
    if str(spec).isdigit():
        return int(spec)
    import sounddevice as sd

    key = "max_input_channels" if kind == "input" else "max_output_channels"
    for i, d in enumerate(sd.query_devices()):
        if str(spec).lower() in d["name"].lower() and d[key] > 0:
            return i
    raise ValueError(f"No {kind} device matching {spec!r}. Run: python -m iriis --list-devices")
