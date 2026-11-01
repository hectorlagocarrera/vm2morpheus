from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from ..models import VMInfo

ProgressCb = Callable[[float, float | None], None]


class Source(ABC):
    """A hypervisor we can read a VM from."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def list_vms(self) -> list[dict]:
        """Every VM on the source as {name, state, cpu, memory_mb}, sorted by name."""

    @abstractmethod
    def get_vm(self, name: str) -> VMInfo: ...

    @abstractmethod
    def power_off(self, name: str) -> None: ...

    @abstractmethod
    def fetch_disks(self, vm: VMInfo, dest: Path, progress: Callable[[str], ProgressCb]) -> list[Path]:
        """Copy every disk of the (powered-off) VM into dest; returns the local files, boot disk first."""

    supports_hot = False  # can copy the disks without powering the VM off (checkpoint/snapshot based)
    hot_type = ""         # what kind of consistency the hot copy gives, for the log

    def hot_prepare(self, vm: VMInfo) -> list:
        """Freeze a consistent point in time (a temporary checkpoint) and return the disks to copy, which no
        longer change while it exists. Only sources with supports_hot implement it."""
        raise NotImplementedError

    def hot_release(self, log=print) -> None:
        """Remove whatever hot_prepare created. Must be safe to call when nothing was created."""

    supports_stream = False  # open_disk is implemented: a disk can be read as a stream, without a local copy

    def open_disk(self, vm: VMInfo, disk):
        """(reader, qemu format, size in bytes) for one disk of a powered-off or checkpointed VM. The reader has
        read(n) and close(). Used to send data disks straight to the HVM host."""
        raise NotImplementedError

    timing_kind = "default"  # which history the progress bar learns from (see timings.py)
    direct = False  # True when fetch_disks returns paths on the source instead of local copies

    def ensure_access(self, paths: list[Path]) -> None:
        """Called before converting; sources with direct reads (re)open access to their paths here."""

    def close(self) -> None:
        pass
