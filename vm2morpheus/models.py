from dataclasses import dataclass, field


@dataclass
class DiskInfo:
    name: str
    size_bytes: int
    path: str


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
