import os
import sys
import time


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
            raise SystemExit(f"environment variable {name} is not set")
        return os.environ[name]
    return value


class Progress:
    """Callable (done, total) -> None that prints a throttled one-line progress bar."""

    def __init__(self, label: str, stream=None, percent: bool = False):
        self.label = label
        self.percent = percent
        self.stream = stream or sys.stderr
        self._last = 0.0

    def __call__(self, done: float, total: float | None = None):
        now = time.monotonic()
        if now - self._last < 0.5 and not (total and done >= total):
            return
        self._last = now
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
