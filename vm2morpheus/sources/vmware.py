import re
import ssl
import sys
import time
import urllib.parse
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


READ_TIMEOUT = 120  # seconds without a single byte from the ESXi before giving up


def vmdk_stream_complete(tail: bytes) -> bool:
    """A stream-optimized VMDK ends with: footer marker, footer (a copy of the header, starting with 'KDMV') and a
    512-byte end-of-stream marker of zeros. Seeing that tail means every byte has arrived, even if the server never
    closes the connection (the export does not announce its length)."""
    return len(tail) >= 1024 and tail[-1024:-1020] == b"KDMV" and not any(tail[-512:])


def file_is_complete_vmdk(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 1024:
            return False
        with open(path, "rb") as f:
            f.seek(size - 1024)
            return vmdk_stream_complete(f.read(1024))
    except OSError:
        return False


def download_to(url: str, local: Path, cookie: str, verify: bool, cb, tick=lambda: None, getter=requests.get) -> int:
    """Stream one exported disk to a file. It stops as soon as the end-of-stream marker has arrived, and a stalled
    connection raises after READ_TIMEOUT instead of waiting forever."""
    done, tail = 0, b""
    try:
        with getter(url, stream=True, verify=verify, headers={"Cookie": cookie}, timeout=(15, READ_TIMEOUT)) as r:
            r.raise_for_status()
            with open(local, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    tail = (tail + chunk)[-1024:]
                    cb(done, None)
                    tick()
                    if vmdk_stream_complete(tail):
                        break  # the server may keep the connection open: do not wait for more
    except requests.exceptions.ReadTimeout as e:
        if vmdk_stream_complete(tail):
            return done
        raise RuntimeError(f"El ESXi dejó de enviar datos durante {READ_TIMEOUT} s (se habían descargado "
                           f"{done / 2**30:.1f} GiB). Revisa la red o el estado del host y vuelve a intentarlo.") from e
    return done


def spread_committed(disks, committed: int) -> None:
    """Datastore space the VM really uses (summary.storage.committed) shared over its disks by size. It is the best
    guess available for how much an export will carry; stream-optimized VMDKs are compressed, so it is an upper bound."""
    total = sum(d.size_bytes for d in disks)
    if committed and total:
        for d in disks:
            d.used_bytes = int(committed * d.size_bytes / total)


def flat_url(host: str, backing_file: str, dc_path: str) -> str:
    """Datastore-browser URL of a disk's raw extent: '[ds1] dir/vm.vmdk' -> https://host/folder/dir/vm-flat.vmdk?..."""
    m = re.match(r"\[(.+?)\]\s*(.+)\.vmdk$", backing_file)
    if not m:
        raise RuntimeError(f"No se entiende la ruta del disco en el almacén: {backing_file}")
    datastore, rel = m.groups()
    q = urllib.parse.quote
    return f"https://{host}/folder/{q(rel)}-flat.vmdk?dcPath={q(dc_path)}&dsName={q(datastore)}"


def measure_download(getter, url: str, seconds: float = 20, clock=time.monotonic, **kw) -> tuple[int, float]:
    """Read from `url` for about `seconds`, discarding the data. Returns (bytes, seconds)."""
    start, total = clock(), 0
    with getter(url, stream=True, timeout=(15, READ_TIMEOUT), **kw) as r:
        r.raise_for_status()
        for chunk in r.iter_content(CHUNK):
            total += len(chunk)
            if clock() - start >= seconds:
                break
    return total, max(clock() - start, 1e-6)


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

    def _datacenter_path(self, mo) -> str:
        """dcPath for the datastore browser: 'ha-datacenter' on a standalone ESXi, the datacenter name on vCenter."""
        from pyVmomi import vim
        node = mo
        while node is not None and not isinstance(node, vim.Datacenter):
            node = getattr(node, "parent", None)
        return node.name if node is not None else "ha-datacenter"

    def speed_test(self, name: str, seconds: float = 20) -> dict:
        """Compare what the program does (compressed export) with a plain datastore download, same code, no disk."""
        from pyVmomi import vim
        vm = self.get_vm(name)
        mo = self._find(name)
        out = {}
        try:
            url = flat_url(self.host, vm.disks[0].path, self._datacenter_path(mo))
            b, t = measure_download(requests.get, url, seconds, auth=(self.user, self.password), verify=self.verify)
            out["flat"] = {"mib_s": b / t / 2**20, "mib": b / 2**20}
        except Exception as e:
            out["flat"] = {"error": str(e)}
        if vm.power_state == "on":
            out["export"] = {"error": "la VM está encendida: la exportación solo funciona con la VM apagada"}
            return out
        lease = None
        try:
            lease = mo.ExportVm()
            while lease.state == vim.HttpNfcLease.State.initializing:
                time.sleep(1)
            if lease.state == vim.HttpNfcLease.State.error:
                raise RuntimeError(str(lease.error))
            du = next(d for d in lease.info.deviceUrl if d.disk)
            esxi = str(getattr(mo.runtime.host, "name", "") or "")
            b, t = measure_download(requests.get, lease_url(du.url, self.api_type, esxi, self.host), seconds,
                                    headers={"Cookie": self.si._stub.cookie}, verify=self.verify)
            out["export"] = {"mib_s": b / t / 2**20, "mib": b / 2**20}
        except Exception as e:
            out["export"] = {"error": str(explain_error(e))}
        finally:
            if lease is not None:
                try:
                    lease.HttpNfcLeaseAbort()
                except Exception:
                    pass
        return out

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
        spread_committed(disks, int(getattr(getattr(vm.summary, "storage", None), "committed", 0) or 0))
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
        wanted = {d.index for d in vm.disks}
        reusable = {i: dest / f"{vm.name}-disk{i}.vmdk" for i in wanted}
        if all(file_is_complete_vmdk(p) for p in reusable.values()):  # e.g. a previous run that hung at the very end
            print("Los discos ya estaban descargados por completo: se reutilizan sin volver a bajarlos.",
                  file=sys.stderr)
            return [reusable[d.index] for d in vm.disks]
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
                disk = next(d for d in vm.disks if d.index == i)
                cb = progress(f"descargar {local.name}", estimate=disk.used_bytes)
                pct = min(99, int(100 * (i + 0.5) / len(disk_urls)))
                download_to(url, local, cookie, self.verify, cb, lambda pct=pct: keep_alive.tick(pct))
                cb.finish()
                out.append(local)
            lease.HttpNfcLeaseComplete()
        except Exception:
            lease.HttpNfcLeaseAbort()
            raise
        return out
