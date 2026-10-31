import os
import shutil
import string
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path


class CancelledError(Exception):
    """The user pressed Cancel."""


def no_window(platform: str | None = None) -> dict:
    """subprocess kwargs that stop Windows from flashing a console window when the app itself has none."""
    return {"creationflags": 0x08000000} if (platform or sys.platform) == "win32" else {}  # CREATE_NO_WINDOW


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


class StallWatchdog:
    """Reports where a worker thread is waiting when its progress counter stops moving.

    A transfer that freezes gives no error and no clue; the stack of the frozen thread (network read, file write,
    a SOAP call...) tells which of them it is. Reports are written to stderr, so they reach the GUI log."""

    def __init__(self, label: str, after: float = 45.0, repeat: float = 60.0, poll: float = 5.0, stream=None,
                 clock=time.monotonic, frames=sys._current_frames):
        self.label, self.after, self.repeat, self.poll = label, after, repeat, poll
        self.stream, self.clock, self.frames = stream, clock, frames
        self.value, self.tid = None, None
        self.changed = clock()
        self.reported = float("-inf")  # nothing reported yet: the first report is due as soon as `after` passes
        self._stop = threading.Event()

    def start(self):
        self.tid = threading.get_ident()  # the thread that is being watched
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def beat(self, value) -> None:
        if value != self.value:
            self.value, self.changed = value, self.clock()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> bool:
        now = self.clock()
        if now - self.changed < self.after or now - self.reported < self.repeat:
            return False
        self.reported = now
        frame = self.frames().get(self.tid)
        where = "".join(traceback.format_stack(frame)[-3:]).rstrip() if frame is not None else "(no se pudo leer)"
        print(f"Aviso: {self.label} lleva {now - self.changed:.0f} s sin recibir datos. El programa está esperando en:\n"
              f"{where}", file=self.stream or sys.stderr)
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.poll):
            self.check()


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min {seconds % 60:02d} s" if seconds % 60 else f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


class Progress:
    """Callable (done, total) -> None that prints a throttled one-line progress bar with speed and time left.

    When the real total is unknown (an ESXi export is compressed on the fly) pass `estimate`, the expected size:
    the time left is then shown as an estimate and the percentage never reaches 100% on its own.

    `Progress.observer`, if set, is called as observer(label, done, total, unit) on every printed update (unit is
    "bytes" or "percent"); the GUI uses it to draw the live throughput chart."""

    observer = None

    def __init__(self, label: str, stream=None, percent: bool = False, cancel=None, on_progress=None,
                 estimate: float | None = None):
        self.label = label
        self.cancel = cancel
        self.on_progress = on_progress
        self.percent = percent
        self.estimate = estimate
        self.stream = stream or sys.stderr
        self._last = 0.0
        self._start = None
        self._done = 0.0
        self._total = None

    def __call__(self, done: float, total: float | None = None):
        if self.cancel is not None and self.cancel.is_set():
            raise CancelledError("cancelled")
        now = time.monotonic()
        if self._start is None:
            self._start = now
        self._done, self._total = done, total
        if now - self._last < 0.5 and not (total and done >= total):
            return
        self._last = now
        fraction = done / total if total else (min(0.99, done / self.estimate) if self.estimate else None)
        if fraction is not None and self.on_progress:
            self.on_progress(min(1.0, fraction))
        if Progress.observer is not None:
            Progress.observer(self.label, done, total, "percent" if self.percent else "bytes")
        eta = self._eta(done, total, now)
        if total and self.percent:
            self.stream.write(f"\r  {self.label}: {done / total:6.1%}{eta}   ")
        elif total:
            self.stream.write(f"\r  {self.label}: {done / total:6.1%} ({human(done)} / {human(total)})"
                              f"{self._speed(done, now)}{eta}   ")
        else:
            self.stream.write(f"\r  {self.label}: {human(done)}{self._speed(done, now)}{eta}   ")
        self.stream.flush()

    def _elapsed(self, now: float) -> float:
        return now - (self._start if self._start is not None else now)

    def _speed(self, done: float, now: float) -> str:
        elapsed = self._elapsed(now)
        return f" · {human(done / elapsed)}/s" if elapsed >= 1 else ""

    def _eta(self, done: float, total: float | None, now: float) -> str:
        """' · ≈ 3 min restantes' once there are a few seconds of data to extrapolate from."""
        elapsed = self._elapsed(now)
        if elapsed < 5 or done <= 0:
            return ""
        target, guessed = (total, False) if total else (self.estimate, True)
        if not target or done >= target:
            return ""
        remaining = (target - done) / (done / elapsed)
        if guessed:  # the real size is unknown and usually smaller (compressed), so this is a ceiling
            return f" · como mucho ≈ {format_duration(remaining)} más (estimación)"
        return f" · ≈ {format_duration(remaining)} restantes"

    def finish(self):
        """Close the line with a summary, so it is clear the transfer ended (and how fast it was)."""
        elapsed = time.monotonic() - self._start if self._start is not None else 0
        if self._start is None:
            self.stream.write("\n")
        elif self.percent:
            self.stream.write(f"\r  {self.label}: 100% · terminado en {format_duration(elapsed)}   \n")
        else:
            speed = f" · {human(self._done / elapsed)}/s" if elapsed >= 1 else ""
            self.stream.write(f"\r  {self.label}: {human(self._done)}{speed} · terminado en "
                              f"{format_duration(elapsed)}   \n")
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
