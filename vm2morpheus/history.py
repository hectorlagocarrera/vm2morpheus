"""History of migrations: one record per GUI run, kept in ~/.vm2morpheus/history.json.

A record holds what is useful afterwards: when, which VM, from where to where, how long every step took, the
average speed of the download and the upload, a down-sampled throughput curve and, if it failed, why."""
import csv
import io
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

PATH = Path.home() / ".vm2morpheus" / "history.json"
MAX_ENTRIES = 200
MAX_POINTS = 150
MIB = 1024 * 1024


def load() -> list[dict]:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(entries: list[dict]) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PATH)  # atomic: a crash never leaves a half-written history


def add(entry: dict) -> None:
    try:
        _save((load() + [entry])[-MAX_ENTRIES:])
    except OSError:
        pass  # the history is a convenience: never fail a migration because of it


def get(entry_id: str) -> dict | None:
    return next((e for e in load() if e.get("id") == entry_id), None)


def update(entry_id: str, **fields) -> bool:
    entries = load()
    for e in entries:
        if e.get("id") == entry_id:
            e.update(fields)
            try:
                _save(entries)
            except OSError:
                return False
            return True
    return False


def delete(entry_id: str) -> bool:
    entries = load()
    kept = [e for e in entries if e.get("id") != entry_id]
    if len(kept) == len(entries):
        return False
    _save(kept)
    return True


def clear() -> None:
    _save([])


def summary_of(entry: dict) -> dict:
    """The entry without its (bulky) curve, for the list view."""
    return {k: v for k, v in entry.items() if k != "series"}


def summarize(entries: list[dict]) -> dict:
    ok = [e for e in entries if e.get("status") == "ok"]
    speeds = lambda key: [e["speeds_mib_s"][key] for e in ok if e.get("speeds_mib_s", {}).get(key)]  # noqa: E731
    avg = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731
    return {
        "runs": len(entries), "ok": len(ok),
        "gib_moved": sum(e.get("bytes", {}).get("upload", 0) for e in ok) / 1024 ** 3,
        "avg_download": avg(speeds("fetch")), "avg_upload": avg(speeds("upload")),
        "avg_seconds": avg([e["total_seconds"] for e in ok if e.get("total_seconds")]),
    }


def downsample(points: list[dict], limit: int = MAX_POINTS) -> list[dict]:
    """Average consecutive samples into at most `limit` points, keeping the phase (download/upload) of each."""
    if len(points) <= limit:
        return points
    size = -(-len(points) // limit)
    out = []
    for i in range(0, len(points), size):
        chunk = points[i:i + size]
        out.append({"x": sum(p["x"] for p in chunk) / len(chunk), "y": sum(p["y"] for p in chunk) / len(chunk),
                    "up": chunk[-1]["up"]})
    return out


def to_csv(entries: list[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["fecha", "estado", "vm_origen", "nombre_en_morpheus", "origen", "tamano_GiB", "duracion_s",
                "descarga_MiB_s", "subida_MiB_s", "instancia_id", "imagen_borrada", "error"])
    for e in entries:
        sp = e.get("speeds_mib_s", {})
        w.writerow([e.get("started", ""), e.get("status", ""), e.get("vm", ""), e.get("target_name", ""),
                    f"{e.get('source_type', '')}:{e.get('source_host', '')}", f"{e.get('gib', 0):.2f}",
                    round(e.get("total_seconds", 0)), f"{sp.get('fetch', 0):.1f}", f"{sp.get('upload', 0):.1f}",
                    (e.get("instance") or {}).get("id", ""), "sí" if e.get("image_deleted") else "no",
                    e.get("error") or ""])
    return out.getvalue()


class Recorder:
    """Collects the transfer samples of a run and turns them into a history entry when it ends."""

    def __init__(self, meta: dict, clock=time.time):
        self.meta, self.clock = dict(meta), clock
        self.started = clock()
        self.samples: list[tuple[float, str, float, str]] = []

    def sample(self, label: str, done: float, total, unit: str) -> None:
        self.samples.append((self.clock(), label, done, unit))

    def _curve(self) -> tuple[list[dict], dict]:
        """Throughput curve (MiB/s over time, bytes transfers only) and bytes moved per kind of step."""
        series, last, moved = [], {}, {}
        for t, label, done, unit in self.samples:
            if unit != "bytes":
                continue
            kind = "upload" if label.startswith("subir") else "fetch" if label.startswith("descargar") else None
            if label in last and t > last[label][0]:
                dt, dd = t - last[label][0], done - last[label][1]
                series.append({"x": round(t - self.started, 2), "y": round(max(0.0, dd / dt / MIB), 3),
                               "up": kind == "upload"})
            last[label] = (t, done)
            if kind:
                moved[kind] = max(moved.get(kind, 0), done)  # the largest value seen per kind (a retry restarts at 0)
        return downsample(series), moved

    def finish(self, status: str, durations: dict, error: str | None = None, result: dict | None = None) -> dict:
        series, moved = self._curve()
        durations = {k: round(v, 1) for k, v in (durations or {}).items()}
        speeds = {k: round(moved[k] / MIB / durations[k], 2) for k in moved if durations.get(k, 0) >= 1}
        result = result or {}
        entry = {
            "id": uuid.uuid4().hex[:8], "status": status,
            "started": datetime.fromtimestamp(self.started).isoformat(timespec="seconds"),
            "total_seconds": round(self.clock() - self.started, 1),
            "steps": durations, "bytes": moved, "speeds_mib_s": speeds, "series": series,
            "error": error, "instance": result.get("instance"), "images": result.get("images", []),
            "image_deleted": False, "workdir_removed": False, **self.meta,
        }
        add(entry)
        return entry
