import os
import shutil
import string
import sys
import tempfile
import time
from pathlib import Path


class CancelledError(Exception):
    """The user pressed Cancel."""


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024


def redact(o):
    """Hide anything that looks like a credential before the JSON is shown (and maybe pasted)."""
    if isinstance(o, dict):
        return {k: "***" if any(w in k.lower() for w in ("password", "secret", "token", "privatekey")) else redact(v)
                for k, v in o.items()}
    if isinstance(o, list):
        return [redact(v) for v in o]
    return o


def resolve_secret(value):
    """'env:NAME' reads the secret from the environment; anything else is returned as-is."""
    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:]
        if name not in os.environ:
            raise SystemExit(f"La variable de entorno {name} no está definida")
        return os.environ[name]
    return value


class Progress:
    """Callable (done, total) -> None that prints a throttled one-line progress bar."""

    def __init__(self, label: str, stream=None, percent: bool = False, cancel=None, on_progress=None):
        self.label = label
        self.cancel = cancel
        self.on_progress = on_progress
        self.percent = percent
        self.stream = stream or sys.stderr
        self._last = 0.0

    def __call__(self, done: float, total: float | None = None):
        if self.cancel is not None and self.cancel.is_set():
            raise CancelledError("cancelled")
        now = time.monotonic()
        if now - self._last < 0.5 and not (total and done >= total):
            return
        self._last = now
        if total and self.on_progress:
            self.on_progress(min(1.0, done / total))
        if total and self.percent:
            self.stream.write(f"\r  {self.label}: {done / total:6.1%}   ")
        elif total:
            self.stream.write(f"\r  {self.label}: {done / total:6.1%} ({human(done)} / {human(total)})   ")
        else:
            self.stream.write(f"\r  {self.label}: {human(done)}   ")
        self.stream.flush()

    def finish(self):
        self.stream.write("\n")
        self.stream.flush()


def _fixed_drive(root: str) -> bool:
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(root) == 3  # DRIVE_FIXED: skip USB, optical and network drives
    except (AttributeError, OSError):
        return True


def pick_workdir() -> str:
    """Work folder on the fixed local disk with the most free space (Windows); the temp dir elsewhere."""
    if os.name == "nt":
        best = None
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root) and _fixed_drive(root):
                try:
                    free = shutil.disk_usage(root).free
                except OSError:
                    continue
                if best is None or free > best[0]:
                    best = (free, root)
        if best:
            return str(Path(best[1]) / "vm2morpheus-work")
    return str(Path(tempfile.gettempdir()) / "vm2morpheus-work")


def browse(path: str | None) -> dict:
    """Folder listing for the GUI picker. Empty path = the disks (with free space) instead of a folder."""
    if not path:
        roots = ([f"{letter}:\\" for letter in string.ascii_uppercase if os.path.exists(f"{letter}:\\")]
                 if os.name == "nt" else ["/", str(Path.home())])
        drives = []
        for root in roots:
            try:
                usage = shutil.disk_usage(root)
                free, total = usage.free, usage.total
            except OSError:
                free = total = None
            drives.append({"path": root, "free": free, "total": total, "local": _fixed_drive(root)})
        return {"path": "", "parent": None, "dirs": [], "drives": drives, "free": None}
    folder = Path(path)
    if not folder.is_dir():
        raise ValueError(f"La carpeta no existe: {path}")
    dirs = []
    try:
        for entry in folder.iterdir():
            try:
                if entry.is_dir() and not entry.name.startswith("$") and entry.name != "System Volume Information":
                    dirs.append(entry.name)
            except OSError:
                continue  # no permission on this entry
    except OSError as e:
        raise ValueError(f"No se puede leer la carpeta: {e}")
    parent = str(folder.parent) if folder.parent != folder else ""
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    return {"path": str(folder), "parent": parent, "dirs": sorted(dirs, key=str.lower)[:500], "drives": [],
            "free": free}
