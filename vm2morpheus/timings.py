"""Remembers how long each step of a migration took, so the global progress bar can weigh the steps realistically.

History is kept per kind of source ("hyperv", "vmware"): a Hyper-V disk copied over SMB and an ESXi export that the
host compresses on the fly differ by an order of magnitude, so blending them would make the bar wrong for both.
Steps that move data (download, convert, upload) are stored as seconds per GiB; the others as plain seconds.
Each new run is blended with the history (moving average), so one unusually slow lab day does not dominate."""
import json
from pathlib import Path

PATH = Path.home() / ".vm2morpheus" / "timings.json"
PER_GIB = ("fetch", "convert", "upload")
ALPHA = 0.5  # weight of the newest run


def _read() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # an older file kept one flat set of steps for every source: it cannot be attributed, so start afresh
    return data if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()) else {}


def load(kind: str = "default") -> dict:
    return _read().get(kind, {})


def predict_weights(steps: list[str], gib: float, kind: str = "default") -> dict | None:
    """Expected duration of each step for a VM of `gib` GiB, or None while there is not enough history."""
    data = load(kind)
    expected = {}
    for step in steps:
        rate = data.get(step)
        if not rate or rate <= 0:
            return None
        expected[step] = rate * max(gib, 0.01) if step in PER_GIB else rate
    return expected


def record(durations: dict, gib: float, kind: str = "default") -> None:
    """Blend the durations measured in this run into the history. Never raises: it is only a convenience."""
    try:
        everything = _read()
        data = everything.setdefault(kind, {})
        for step, seconds in durations.items():
            if seconds <= 0:
                continue
            value = seconds / max(gib, 0.01) if step in PER_GIB else seconds
            old = data.get(step)
            data[step] = value if not old else (1 - ALPHA) * old + ALPHA * value
        PATH.parent.mkdir(parents=True, exist_ok=True)
        PATH.write_text(json.dumps(everything, indent=2), encoding="utf-8")
    except OSError:
        pass
