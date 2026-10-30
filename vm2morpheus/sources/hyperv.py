import json
import time
from pathlib import Path, PureWindowsPath

from ..models import DiskInfo, VMInfo
from .base import ProgressCb, Source

CHUNK = 8 * 1024 * 1024


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class HyperVSource(Source):
    """Reads a VM from a Hyper-V host: metadata over WinRM, disk files over SMB admin shares.

    Needs: WinRM enabled on the host, SMB access to the admin shares (C$ ...), and an
    account that is local admin / Hyper-V administrator.
    """

    def __init__(self, cfg: dict):
        self.host = cfg["host"]
        self.user = cfg["username"]
        self.password = cfg["password"]
        self.transport = cfg.get("transport", "ntlm")
        self.ssl = bool(cfg.get("ssl", False))
        self.verify = bool(cfg.get("verify_ssl", False))
        self.os_family = cfg.get("os_family", "other")
        self.session = None

    def connect(self) -> None:
        import winrm
        scheme, port = ("https", 5986) if self.ssl else ("http", 5985)
        self.session = winrm.Session(
            f"{scheme}://{self.host}:{port}/wsman",
            auth=(self.user, self.password),
            transport=self.transport,
            server_cert_validation="validate" if self.verify else "ignore",
        )
        self._ps("$PSVersionTable.PSVersion.Major")

    def _ps(self, script: str) -> str:
        # progress records come back as CLIXML on stderr and make pywinrm emit a bogus warning
        r = self.session.run_ps("$ProgressPreference='SilentlyContinue'\n" + script)
        if r.status_code != 0:
            raise RuntimeError(f"PowerShell failed on {self.host}: {r.std_err.decode(errors='replace')}")
        return r.std_out.decode(errors="replace").strip()

    def get_vm(self, name: str) -> VMInfo:
        n = ps_quote(name)
        out = self._ps(f"""
$ErrorActionPreference='Stop'
$vm = Get-VM -Name {n}
$disks = @(Get-VMHardDiskDrive -VM $vm | ForEach-Object {{ $v = Get-VHD -Path $_.Path; [pscustomobject]@{{Path=$_.Path; Size=[int64]$v.Size; FileSize=[int64]$v.FileSize}} }})
$nets = @(Get-VMNetworkAdapter -VM $vm | ForEach-Object {{ $_.SwitchName }})
$snaps = @(Get-VMSnapshot -VM $vm).Count
[pscustomobject]@{{Name=$vm.Name; Cpu=[int](Get-VMProcessor -VM $vm).Count; MemoryMB=[int64]($vm.MemoryStartup/1MB); Generation=[int]$vm.Generation; State=$vm.State.ToString(); Disks=$disks; Nets=$nets; Snapshots=[int]$snaps}} | ConvertTo-Json -Depth 4 -Compress
""")
        d = json.loads(out)
        disks = [DiskInfo(name=PureWindowsPath(x["Path"]).name, size_bytes=int(x["Size"]), path=x["Path"],
                          used_bytes=int(x["FileSize"]) if x.get("FileSize") else None)
                 for x in as_list(d.get("Disks"))]
        return VMInfo(
            name=d["Name"], cpu=d["Cpu"], memory_mb=d["MemoryMB"],
            firmware="uefi" if d["Generation"] == 2 else "bios",
            os_family=self.os_family,
            power_state="on" if d["State"] == "Running" else "off",
            disks=disks, networks=[x for x in as_list(d.get("Nets")) if x],
            has_snapshots=d.get("Snapshots", 0) > 0,
        )

    def power_off(self, name: str) -> None:
        n = ps_quote(name)
        self._ps(f"Stop-VM -Name {n} -Force -ErrorAction Stop")  # graceful guest shutdown
        for _ in range(60):
            if self._ps(f"(Get-VM -Name {n}).State.ToString()") == "Off":
                return
            time.sleep(5)
        raise RuntimeError(f"VM {name} did not power off in 5 minutes")

    def fetch_disks(self, vm: VMInfo, dest: Path, progress) -> list[Path]:
        import smbclient
        smbclient.register_session(self.host, username=self.user, password=self.password)
        out = []
        for disk in vm.disks:
            if disk.path.lower().endswith(".avhdx"):
                raise RuntimeError(f"{disk.path} is a checkpoint (differencing) disk; "
                                   "delete or merge the VM's checkpoints first")
            p = PureWindowsPath(disk.path)
            drive = p.drive.rstrip(":")
            unc = "\\\\" + self.host + "\\" + drive + "$\\" + "\\".join(p.parts[1:])
            local = dest / disk.name
            total = smbclient.stat(unc).st_size
            cb: ProgressCb = progress(f"download {disk.name}")
            done = 0
            with smbclient.open_file(unc, mode="rb") as src, open(local, "wb") as dst:
                while chunk := src.read(CHUNK):
                    dst.write(chunk)
                    done += len(chunk)
                    cb(done, total)
            out.append(local)
        return out
