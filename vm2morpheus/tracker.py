"""Overall progress of a migration: which step is running and how far along the whole process is."""
from .pipeline_steps import STEP_LABELS

# Rough share of the total time of each step; renormalised over the steps that apply to a run.
WEIGHTS = {"fetch": 0.30, "convert": 0.25, "drivers": 0.10, "upload": 0.30, "instance": 0.05}


class Tracker:
    def __init__(self, steps: list[str], done: list[str], callback=None):
        self.steps = steps
        self.callback = callback
        self.done = set(done)
        self.current = None
        self.fraction = 0.0
        total = sum(WEIGHTS[s] for s in steps) or 1.0
        self.weight = {s: WEIGHTS[s] / total for s in steps}
        self._emit()

    def overall(self) -> float:
        value = sum(self.weight[s] for s in self.done if s in self.weight)
        if self.current in self.weight and self.current not in self.done:
            value += self.weight[self.current] * self.fraction
        return min(1.0, value)

    def begin(self, step: str) -> None:
        self.current, self.fraction = step, 0.0
        self._emit()

    def update(self, step: str, fraction: float) -> None:
        if step == self.current:
            self.fraction = min(1.0, max(0.0, fraction))
            self._emit()

    def finish(self, step: str) -> None:
        self.done.add(step)
        if self.current == step:
            self.current, self.fraction = None, 0.0
        self._emit()

    def complete(self) -> None:
        self.done.update(self.steps)
        self.current, self.fraction = None, 0.0
        self._emit()

    def _emit(self) -> None:
        if self.callback:
            index = self.steps.index(self.current) + 1 if self.current in self.steps else 0
            self.callback({"step": self.current, "label": STEP_LABELS.get(self.current, ""),
                           "index": index, "total_steps": len(self.steps),
                           "fraction": self.fraction, "overall": self.overall()})
