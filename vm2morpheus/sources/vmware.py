import ssl
import time
from pathlib import Path

import requests

from ..models import DiskInfo, VMInfo
from .base import Source

CHUNK = 4 * 1024 * 1024


class VMwareSource(Source):
    """Reads a VM from vCenter or a standalone ESXi through the vSphere API.

    Disks are pulled with an NFC export lease (the same mechanism as 'Export OVF'),
    which yields stream-optimized VMDKs. The VM must be powered off.
    """

    def __init__(self, cfg: dict):
        self.host = cfg["host"]
        self.user = cfg["username"]
        self.password = cfg["password"]
        self.port = int(cfg.get("port", 443))
        self.verify = bool(cfg.get("verify_ssl", False))
        self.si = None
        self._vms = {}

    def connect(self) -> None:
        from pyVim.connect import SmartConnect
        ctx = ssl.create_default_context() if self.verify else ssl._create_unverified_context()
        self.si = SmartConnect(host=self.host, user=self.user, pwd=self.password,
                               port=self.port, sslContext=ctx)

    def close(self) -> None:
        if self.si:
            from pyVim.connect import Disconnect
            Disconnect(self.si)

    def _find(self, name: str):
        from pyVmomi import vim
        if name in self._vms:
            return self._vms[name]
        content = self.si.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            for v in view.view:
                if v.name == name:
                    self._vms[name] = v
                    return v
        finally:
            view.Destroy()
        raise RuntimeError(f"No se encontró la VM {name!r} en {self.host}")

    def list_vms(self) -> list[dict]:
        from pyVmomi import vim
        content = self.si.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            vms = [{"name": v.name, "state": "on" if v.runtime.powerState == "poweredOn" else "off",
                    "cpu": v.config.hardware.numCPU, "memory_mb": v.config.hardware.memoryMB}
                   for v in view.view if v.config and not v.config.template]
        finally:
            view.Destroy()
        return sorted(vms, key=lambda v: v["name"].lower())

    def get_vm(self, name: str) -> VMInfo:
        from pyVmomi import vim
        vm = self._find(name)
        cfg = vm.config
        disks, nets = [], []
        for dev in cfg.hardware.device:
            if isinstance(dev, vim.vm.device.VirtualDisk):
                disks.append(DiskInfo(name=dev.deviceInfo.label, size_bytes=int(dev.capacityInBytes),
                                      path=dev.backing.fileName))
            elif isinstance(dev, vim.vm.device.VirtualEthernetCard):
                nets.append(dev.deviceInfo.summary)
        guest = (cfg.guestId or "").lower()
        os_family = "windows" if guest.startswith("windows") else "other" if not guest else "linux"
        return VMInfo(
            name=vm.name, cpu=cfg.hardware.numCPU, memory_mb=cfg.hardware.memoryMB,
            firmware="uefi" if cfg.firmware == "efi" else "bios",
            os_family=os_family,
            power_state="on" if vm.runtime.powerState == "poweredOn" else "off",
            disks=disks, networks=nets,
            has_snapshots=vm.snapshot is not None,
        )

    def power_off(self, name: str) -> None:
        vm = self._find(name)
        if vm.runtime.powerState != "poweredOn":
            return
        try:
            vm.ShutdownGuest()
        except Exception:
            vm.PowerOffVM_Task()
        for i in range(60):
            if vm.runtime.powerState == "poweredOff":
                return
            if i == 24:  # guest ignored the shutdown request; pull the plug
                try:
                    vm.PowerOffVM_Task()
                except Exception:
                    pass
            time.sleep(5)
        raise RuntimeError(f"La VM {name} no se apagó en 5 minutos")

    def fetch_disks(self, vm: VMInfo, dest: Path, progress) -> list[Path]:
        from pyVmomi import vim
        mo = self._find(vm.name)
        lease = mo.ExportVm()
        while lease.state == vim.HttpNfcLease.State.initializing:
            time.sleep(1)
        if lease.state == vim.HttpNfcLease.State.error:
            raise RuntimeError(f"Falló la exportación de la VM: {lease.error}")
        out = []
        try:
            cookie = self.si._stub.cookie
            disk_urls = [d for d in lease.info.deviceUrl if d.disk]
            for i, du in enumerate(disk_urls):
                url = du.url.replace("*", self.host)
                local = dest / f"{vm.name}-disk{i}.vmdk"
                total = vm.disks[i].size_bytes if i < len(vm.disks) else None
                cb = progress(f"descargar {local.name}")
                done = 0
                with requests.get(url, stream=True, verify=self.verify, headers={"Cookie": cookie}) as r:
                    r.raise_for_status()
                    with open(local, "wb") as f:
                        for chunk in r.iter_content(CHUNK):
                            f.write(chunk)
                            done += len(chunk)
                            cb(done, None)
                            lease.HttpNfcLeaseProgress(min(99, int(100 * (i + 0.5) / len(disk_urls))))
                out.append(local)
            lease.HttpNfcLeaseComplete()
        except Exception:
            lease.HttpNfcLeaseAbort()
            raise
        return out
