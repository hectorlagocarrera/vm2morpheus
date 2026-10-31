from dataclasses import dataclass, field


@dataclass
class DiskInfo:
    name: str
    size_bytes: int
    path: str
    used_bytes: int | None = None  # actual file size on the source, when known
    index: int | None = None  # position on the source VM, kept when only some disks are selected


@dataclass
class VMInfo:
    name: str
    cpu: int
    memory_mb: int
    firmware: str  # "bios" | "uefi"
    os_family: str  # "windows" | "linux" | "other"
    power_state: str  # "on" | "off"
    disks: list[DiskInfo]
    networks: list[str] = field(default_factory=list)
    has_snapshots: bool = False
    snapshot_names: list[str] = field(default_factory=list)
