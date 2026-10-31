"""Overall progress of a migration: which step is running and how far along the whole process is.

The global percentage is an estimate: each step gets a share of the total. The default shares are rough; once a
migration has been timed (see timings.py) the shares come from the measured durations instead."""
import time

from .pipeline_steps import STEP_LABELS

# Default share of the total time of each step; renormalised over the steps that apply to a run.
WEIGHTS = {"fetch": 0.30, "convert": 0.25, "drivers": 0.10, "upload": 0.30, "instance": 0.05}


class Tracker:
    def __init__(self, steps: list[str], done: list[str], callback=None, weights: dict | None = None,
                 clock=time.monotonic):
        self.steps = steps
        self.callback = callback
        self.clock = clock
        self.durations: dict[str, float] = {}  # seconds actually spent in each step during this run
        self._since = None
        self.done = set(done)
        self.current = None
        self.fraction = 0.0
        self.detail, self.shown = None, None
        base = weights if weights and all(weights.get(s, 0) > 0 for s in steps) else WEIGHTS
        total = sum(base[s] for s in steps) or 1.0
        self.weight = {s: base[s] / total for s in steps}
        self._emit()

    def overall(self) -> float:
        value = sum(self.weight[s] for s in self.done if s in self.weight)
        if self.current in self.weight and self.current not in self.done:
            value += self.weight[self.current] * self.fraction
        return min(1.0, value)

    def _stop_clock(self) -> None:
        """Book the time since the last switch to the step that was running (steps can interleave: the driver
        injection happens inside the conversion step)."""
        now = self.clock()
        if self.current is not None and self._since is not None:
            self.durations[self.current] = self.durations.get(self.current, 0.0) + (now - self._since)
        self._since = None

    def snapshot(self) -> dict:
        """Durations so far, including the step that is running now (for a run that failed or was cancelled)."""
        d = dict(self.durations)
        if self.current is not None and self._since is not None:
            d[self.current] = d.get(self.current, 0.0) + (self.clock() - self._since)
        return d

    def begin(self, step: str) -> None:
        self._stop_clock()
        self._since = self.clock()
        self.current, self.fraction = step, 0.0
        self.detail, self.shown = None, None
        self._emit()

    def update(self, step: str, fraction: float, detail: str | None = None, shown: float | None = None,
               hide_percent: bool = False) -> None:
        """fraction: progress of the whole step (feeds the global bar). detail/shown: what the user is looking at
        right now, e.g. "convirtiendo el disco a VHDX" at 78%, so the bar and the log line agree."""
        if step == self.current:
            self.fraction = min(1.0, max(0.0, fraction))
            self.detail = detail
            self.shown = None if hide_percent else (self.fraction if shown is None else min(1.0, max(0.0, shown)))
            self._emit()

    def finish(self, step: str) -> None:
        if self.current == step:
            self._stop_clock()
        self.done.add(step)
        if self.current == step:
            self.current, self.fraction = None, 0.0
            self.detail, self.shown = None, None
        self._emit()

    def complete(self) -> None:
        self._stop_clock()
        self.done.update(self.steps)
        self.current, self.fraction = None, 0.0
        self.detail, self.shown = None, None
        self._emit()

    def _emit(self) -> None:
        if self.callback:
            index = self.steps.index(self.current) + 1 if self.current in self.steps else 0
            label = STEP_LABELS.get(self.current, "")
            if label and self.detail:
                label += f" · {self.detail}"
            fraction = self.fraction if self.detail is None or self.shown is not None else None  # None: no percentage
            if self.shown is not None:
                fraction = self.shown
            self.callback({"step": self.current, "label": label, "index": index, "total_steps": len(self.steps),
                           "fraction": fraction, "overall": self.overall()})
