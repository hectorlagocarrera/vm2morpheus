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
    def get_vm(self, name: str) -> VMInfo: ...

    @abstractmethod
    def power_off(self, name: str) -> None: ...

    @abstractmethod
    def fetch_disks(self, vm: VMInfo, dest: Path, progress: Callable[[str], ProgressCb]) -> list[Path]:
        """Copy every disk of the (powered-off) VM into dest; returns the local files, boot disk first."""

    direct = False  # True when fetch_disks returns paths on the source instead of local copies

    def ensure_access(self, paths: list[Path]) -> None:
        """Called before converting; sources with direct reads (re)open access to their paths here."""

    def close(self) -> None:
        pass
