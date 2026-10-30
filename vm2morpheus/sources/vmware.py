import ssl
import sys
import time
from pathlib import Path

import requests

from ..models import DiskInfo, VMInfo
from .base import Source

CHUNK = 8 * 1024 * 1024


class LeaseKeepAlive:
    """The export lease only needs a progress ping every few seconds to stay alive (it expires after minutes of
    silence). Pinging after every chunk meant one extra SOAP round trip per 4 MiB, which throttled the download."""

    def __init__(self, lease, interval: float = 20.0, clock=time.monotonic):
        self.lease, self.interval, self.clock = lease, interval, clock
        self.last = clock()

    def tick(self, percent: int) -> None:
        now = self.clock()
        if now - self.last >= self.interval:
            self.lease.HttpNfcLeaseProgress(percent)
            self.last = now


def note_unverified_tls(verify: bool, host: str, stream=None) -> bool:
    """ESXi/vCenter normally use a self-signed certificate, so verification is off unless the user asks for it.
    Say so once, in Spanish, instead of letting urllib3 print its English warning for every request."""
    if verify:
        return False
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    print(f"Aviso: no se verifica el certificado TLS de {host} (suele ser autofirmado). "
          "Marca 'Verificar el certificado del origen' si el tuyo es válido.", file=stream or sys.stderr)
    return True


def explain_error(e: Exception) -> Exception:
    """Standalone ESXi with a free license answers write operations with RestrictedVersion; say so clearly."""
    text = f"{type(e).__name__} {e}".lower()
    if "restrictedversion" in text or "license" in text and "prohibit" in text:
        return RuntimeError("La licencia de este ESXi no permite la operación por API (la licencia gratuita es de "
                            "solo lectura). Usa una licencia que lo permita o conéctate a través de vCenter. "
                            f"Detalle: {e}")
    return e


def lease_url(url: str, api_type: str, esxi_host: str | None, connect_host: str) -> str:
    """NFC export URLs carry a '*' where the host name goes. Connected to a standalone ESXi that is the host we
    connected to; connected to vCenter the data comes straight from the ESXi host that runs the VM."""
    host = esxi_host if api_type == "VirtualCenter" and esxi_host else connect_host
    return url.replace("*", host)


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
        self.api_type = ""
        self._vms = {}

    def connect(self) -> None:
        from pyVim.connect import SmartConnect
        ctx = ssl.create_default_context() if self.verify else ssl._create_unverified_context()
        self.si = SmartConnect(host=self.host, user=self.user, pwd=self.password,
                               port=self.port, sslContext=ctx)
        note_unverified_tls(self.verify, self.host)
        self.api_type = self.si.content.about.apiType  # "VirtualCenter" or "HostAgent" (standalone ESXi)

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
            matches = [v for v in view.view if v.name == name]
        finally:
            view.Destroy()
        if not matches:
            raise RuntimeError(f"No se encontró la VM {name!r} en {self.host}")
        if len(matches) > 1:  # vCenter allows the same name in different folders / datacenters
            hosts = ", ".join(sorted({str(getattr(v.runtime.host, "name", "?")) for v in matches}))
            raise RuntimeError(f"Hay {len(matches)} VMs llamadas {name!r} (en los hosts: {hosts}). "
                               "Renombra una o usa un nombre único para poder elegirla sin ambigüedad.")
        self._vms[name] = matches[0]
        return matches[0]

    def kind(self) -> str:
        return "vCenter" if self.api_type == "VirtualCenter" else "ESXi"

    def list_vms(self) -> list[dict]:
        from pyVmomi import vim
        content = self.si.RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            vms = [{"name": v.name, "state": "on" if v.runtime.powerState == "poweredOn" else "off",
                    "cpu": v.config.hardware.numCPU, "memory_mb": v.config.hardware.memoryMB,
                    "host": str(getattr(v.runtime.host, "name", "") or "")}
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
                                      path=dev.backing.fileName, index=len(disks)))
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
        try:
            self._power_off(name)
        except RuntimeError:
            raise
        except Exception as e:
            raise explain_error(e)

    def _power_off(self, name: str) -> None:
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
        try:
            return self._fetch_disks(vm, dest, progress)
        except RuntimeError:
            raise
        except Exception as e:
            raise explain_error(e)

    def _fetch_disks(self, vm: VMInfo, dest: Path, progress) -> list[Path]:
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
            esxi_host = str(getattr(mo.runtime.host, "name", "") or "")
            disk_urls = [d for d in lease.info.deviceUrl if d.disk]
            wanted = {d.index for d in vm.disks}
            keep_alive = LeaseKeepAlive(lease)
            for i, du in enumerate(disk_urls):
                if i not in wanted:  # the user left this disk out
                    continue
                url = lease_url(du.url, self.api_type, esxi_host, self.host)
                local = dest / f"{vm.name}-disk{i}.vmdk"
                cb = progress(f"descargar {local.name}")
                done = 0
                with requests.get(url, stream=True, verify=self.verify, headers={"Cookie": cookie}) as r:
                    r.raise_for_status()
                    with open(local, "wb") as f:
                        for chunk in r.iter_content(CHUNK):
                            f.write(chunk)
                            done += len(chunk)
                            cb(done, None)
                            keep_alive.tick(min(99, int(100 * (i + 0.5) / len(disk_urls))))
                out.append(local)
            lease.HttpNfcLeaseComplete()
        except Exception:
            lease.HttpNfcLeaseAbort()
            raise
        return out
