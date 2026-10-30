import os
import re
import threading
import shutil
import subprocess
import sys
from pathlib import Path

from .util import CancelledError, no_window

PCT = re.compile(rb"\((\d+(?:\.\d+)?)/100%\)")


def find_tool(name: str) -> str | None:
    if getattr(sys, "frozen", False):  # packaged .exe: qemu-img sits in a "qemu" folder next to it
        exe = Path(sys.executable).parent / "qemu" / (name + (".exe" if os.name == "nt" else ""))
        if exe.exists():
            return str(exe)
    found = shutil.which(name)
    if found:
        return found
    for d in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):  # QEMU on Windows
        exe = Path(d, "qemu", name + ".exe")
        if d and exe.exists():
            return str(exe)
    return None


def require_tool(name: str) -> str:
    path = find_tool(name)
    if not path:
        raise RuntimeError(f"No se encuentra {name} en el PATH (instala qemu-utils / virt-v2v)")
    return path


def convert_disk(src: Path, dst: Path, fmt: str = "qcow2", progress=None, cancel=None) -> Path:
    """qemu-img convert; the input format is auto-detected (vhdx, vmdk, vhd, raw ...)."""
    qemu_img = require_tool("qemu-img")
    if cancel is not None and cancel.is_set():
        raise CancelledError("cancelled")
    proc = subprocess.Popen([qemu_img, "convert", "-p", "-O", fmt, str(src), str(dst)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **no_window())
    finished = threading.Event()

    def watch():  # kill qemu-img as soon as Cancel is pressed, even while it prints nothing
        while not finished.is_set():
            if cancel.wait(0.3):
                proc.kill()
                return

    if cancel is not None:
        threading.Thread(target=watch, daemon=True).start()
    buf = b""
    while chunk := proc.stdout.read(64):
        buf = (buf + chunk)[-64:]
        m = PCT.findall(buf)
        if m and progress:
            progress(float(m[-1]), 100)
    err = proc.stderr.read().decode(errors="replace")
    code = proc.wait()
    finished.set()
    if cancel is not None and cancel.is_set():
        dst.unlink(missing_ok=True)
        raise CancelledError("cancelled")
    if code != 0:
        raise RuntimeError(f"Falló qemu-img al convertir {src.name}: {err.strip()}")
    return dst


def inject_drivers(disk: Path, vm_name: str, memory_mb: int, cpu: int, uefi: bool, fmt: str = "qcow2"):
    """Best effort: virt-v2v-in-place installs virtio drivers and fixes boot config inside the guest.

    Needs virt-v2v (>= 2.0) and, for Windows guests, the virtio-win package. Experimental.
    """
    require_tool("virt-v2v-in-place")
    os_el = "<os firmware='efi'><type arch='x86_64'>hvm</type></os>" if uefi else "<os><type arch='x86_64'>hvm</type></os>"
    xml = (f"<domain type='kvm'><name>{vm_name}</name><memory unit='MiB'>{memory_mb}</memory>"
           f"<vcpu>{cpu}</vcpu>{os_el}<devices><disk type='file' device='disk'>"
           f"<driver name='qemu' type='{fmt}'/><source file='{disk}'/>"
           f"<target dev='sda' bus='sata'/></disk></devices></domain>")
    xml_path = disk.with_suffix(".xml")
    xml_path.write_text(xml)
    r = subprocess.run(["virt-v2v-in-place", "-i", "libvirtxml", str(xml_path)],
                       capture_output=True, text=True, **no_window())
    if r.returncode != 0:
        raise RuntimeError(f"Falló virt-v2v-in-place: {r.stderr.strip()[-800:]}")
