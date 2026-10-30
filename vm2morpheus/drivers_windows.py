"""Offline VirtIO driver injection into a Windows guest disk, using only Windows built-ins.

The disk (VHD/VHDX) is attached with Mount-DiskImage, the virtio-win ISO is mounted too, and DISM adds the
drivers to the offline Windows image. Must run on Windows as Administrator, on a COPY of the disk.
"""
import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path

import requests

from .util import no_window

DRIVERS = ("viostor", "vioscsi", "NetKVM", "Balloon", "vioserial")
ISO_URL = "https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/stable-virtio/virtio-win.iso"
CACHED_ISO = Path.home() / ".vm2morpheus" / "virtio-win.iso"


SPARSE, COMPRESSED, ENCRYPTED = 0x200, 0x800, 0x4000  # FILE_ATTRIBUTE_* flags


def file_attributes(path: Path) -> int:
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    if attrs == 0xFFFFFFFF:
        raise OSError(f"no se pueden leer los atributos de {path}")
    return attrs


def describe_file(path: Path, attrs=file_attributes) -> str:
    """One line for the log: file system and the attributes Mount-DiskImage cares about."""
    try:
        flags = attrs(path)
    except (OSError, AttributeError):  # AttributeError: not Windows
        return "atributos no legibles"
    fs = "?"
    try:
        buf = ctypes.create_unicode_buffer(32)
        root = os.path.splitdrive(os.path.abspath(str(path)))[0] + "\\"
        ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, buf, 32)
        fs = buf.value or "?"
    except Exception:
        pass
    yes = lambda bit: "sí" if flags & bit else "no"  # noqa: E731
    return (f"sistema de ficheros {fs} · disperso: {yes(SPARSE)} · comprimido: {yes(COMPRESSED)} · "
            f"cifrado: {yes(ENCRYPTED)}")


def rewrite_dense(path: Path) -> None:
    """Copy the file through ordinary reads and writes: holes read back as zeros and are written out as data, so the
    new file is not sparse."""
    tmp = path.with_name(path.name + ".dense")
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
    os.replace(tmp, path)


def make_mountable(path: Path, log=print, attrs=file_attributes, run=subprocess.run) -> None:
    """Mount-DiskImage refuses VHD/VHDX files that are sparse, compressed or encrypted. qemu-img on Windows writes
    sparse files, so the VHDX it just produced is exactly that."""
    quiet = {"capture_output": True, **no_window()}
    log(f"Disco a montar: {path.name} ({describe_file(path, attrs)})")
    for _ in range(3):
        flags = attrs(path)
        if not flags & (SPARSE | COMPRESSED | ENCRYPTED):
            return
        if flags & ENCRYPTED:
            run(["cipher", "/d", str(path)], **quiet)
        if flags & COMPRESSED:
            run(["compact", "/u", str(path)], **quiet)
        if flags & SPARSE:
            log("Quitando el atributo de fichero disperso del disco para poder montarlo ...")
            run(["fsutil", "sparse", "setflag", str(path), "0"], **quiet)
            if attrs(path) & SPARSE:  # holes remain: rewrite the file without them
                rewrite_dense(path)
    raise RuntimeError("No se pudo dejar el disco sin comprimir, sin cifrar y sin dispersar, que es lo que exige "
                       "Windows para montarlo. Prueba con otra carpeta de trabajo en un disco NTFS sin compresión.")


def _clear_attributes(path: Path, run=subprocess.run) -> None:
    quiet = {"capture_output": True, **no_window()}
    for cmd in (["compact", "/u", str(path)], ["cipher", "/d", str(path)], ["fsutil", "sparse", "setflag", str(path), "0"]):
        try:
            run(cmd, **quiet)
        except OSError:
            pass


def mount_disk(ps, image: str, iso: str, disk: Path, log=print, run=subprocess.run) -> str:
    """Mount the disk and the ISO. If Windows answers with the virtual-disk limitation (0xc03a001a) even though
    the attributes looked fine, rewrite the file through ordinary writes, clear any inherited compression and
    try once more."""
    script = _mount_script(image, iso)
    try:
        return ps(script)
    except RuntimeError as e:
        if "c03a001a" not in str(e).lower():
            raise
    log(f"Windows rechazó el disco ({describe_file(disk)}). Se reescribe como fichero normal y se reintenta ...")
    ps(_dismount_script(image, iso))
    rewrite_dense(disk)
    _clear_attributes(disk, run)
    try:
        return ps(script)
    except RuntimeError as e:
        if "c03a001a" in str(e).lower():
            raise RuntimeError(
                f"Windows no puede montar el disco virtual ({describe_file(disk)}). Debe estar en un disco NTFS, "
                "sin comprimir, sin cifrar y sin dispersar; prueba con otra carpeta de trabajo en otro disco. "
                f"Detalle: {e}") from e
        raise


def os_folder(build: int, installation_type: str) -> str | None:
    """Folder name used by the virtio-win ISO for a given Windows build."""
    server = installation_type.lower() == "server"
    if server:
        table = {6001: "2k8", 6002: "2k8", 6003: "2k8", 7601: "2k8R2", 9200: "2k12", 9600: "2k12R2",
                 14393: "2k16", 17763: "2k19", 20348: "2k22"}
        if build in table:
            return table[build]
        return "2k25" if build >= 26100 else None
    if build == 7601:
        return "w7"
    if build == 9200 or build == 9600:
        return "w8.1" if build == 9600 else "w8"
    if 10240 <= build < 22000:
        return "w10"
    return "w11" if build >= 22000 else None


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def run_powershell(script: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, **no_window())
    if r.returncode != 0:
        raise RuntimeError(f"Falló PowerShell: {(r.stderr or r.stdout).strip()[-600:]}")
    return r.stdout.strip()


def run_dism(args: list[str]) -> None:
    r = subprocess.run(["dism", *args], capture_output=True, text=True, **no_window())
    if r.returncode != 0:
        raise RuntimeError(f"Falló DISM ({r.returncode}): {(r.stdout + r.stderr).strip()[-600:]}")


ADMIN = ("if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent())"
         ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'ejecuta vm2morpheus como administrador' }")


def _mount_script(image: str, iso: str) -> str:
    return f"""
$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'
{ADMIN}
$isoVol = Mount-DiskImage -ImagePath {ps_quote(iso)} -PassThru | Get-Volume
Mount-DiskImage -ImagePath {ps_quote(image)} | Out-Null
$disk = Get-DiskImage -ImagePath {ps_quote(image)} | Get-Disk
if ($disk.IsOffline) {{ Set-Disk -Number $disk.Number -IsOffline $false }}
if ($disk.IsReadOnly) {{ Set-Disk -Number $disk.Number -IsReadOnly $false }}
$win = $null
foreach ($p in Get-Partition -DiskNumber $disk.Number) {{
  if (-not $p.DriveLetter) {{ try {{ $p | Add-PartitionAccessPath -AssignDriveLetter }} catch {{}} ; $p = Get-Partition -DiskNumber $disk.Number -PartitionNumber $p.PartitionNumber }}
  if ($p.DriveLetter -and (Test-Path ($p.DriveLetter + ':\\Windows\\System32\\config\\SOFTWARE'))) {{ $win = $p.DriveLetter + ':\\'; break }}
}}
if (-not $win) {{ throw 'no se encontró ninguna instalación de Windows en el disco' }}
reg load HKLM\\VM2M_SOFT ($win + 'Windows\\System32\\config\\SOFTWARE') | Out-Null
$cv = Get-ItemProperty 'HKLM:\\VM2M_SOFT\\Microsoft\\Windows NT\\CurrentVersion'
$build = [int]$cv.CurrentBuildNumber; $type = [string]$cv.InstallationType
$cv = $null; [gc]::Collect(); reg unload HKLM\\VM2M_SOFT | Out-Null
@{{win=$win; iso=($isoVol.DriveLetter + ':\\'); build=$build; type=$type}} | ConvertTo-Json -Compress
"""


def _dismount_script(image: str, iso: str) -> str:
    return (f"$ErrorActionPreference='SilentlyContinue'; Dismount-DiskImage -ImagePath {ps_quote(image)} | Out-Null; "
            f"Dismount-DiskImage -ImagePath {ps_quote(iso)} | Out-Null")


def ensure_virtio_iso(path: str | None, progress=None) -> str:
    """Use the given ISO, or download the stable virtio-win ISO once and cache it."""
    if path:
        if not Path(path).is_file():
            raise RuntimeError(f"No se encuentra el ISO de virtio-win: {path}")
        return path
    if CACHED_ISO.is_file():
        return str(CACHED_ISO)
    CACHED_ISO.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHED_ISO.with_suffix(".part")
    with requests.get(ISO_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("Content-Length", 0)) or None, 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(4 * 1024 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    tmp.replace(CACHED_ISO)
    return str(CACHED_ISO)


def inject_virtio_windows(disk: Path, iso: str, log=print, ps=run_powershell, dism=run_dism,
                          prepare=None) -> list[str]:
    """Add the VirtIO drivers found on the ISO to the Windows installed on `disk`. Returns the driver names added."""
    if os.name != "nt" and ps is run_powershell:
        raise RuntimeError("La inyección de drivers con DISM solo funciona en Windows")
    image = str(Path(disk).resolve())
    if prepare is None:
        prepare = make_mountable if ps is run_powershell else (lambda *a, **k: None)  # fakes in tests skip the OS call
    prepare(Path(disk), log)
    try:
        raw = mount_disk(ps, image, iso, Path(disk), log)
        info = json.loads(raw)
        folder = os_folder(int(info["build"]), info["type"])
        if not folder:
            raise RuntimeError(f"Versión de Windows no soportada para los drivers VirtIO: build {info['build']} ({info['type']})")
        log(f"Windows build {info['build']} ({info['type']}): se usa la carpeta {folder!r} de virtio-win")
        added = []
        for name in DRIVERS:
            d = Path(info["iso"]) / name / folder / "amd64"
            if d.exists():
                dism([f"/Image:{info['win']}", "/Add-Driver", f"/Driver:{d}", "/Recurse"])
                added.append(name)
        if "viostor" not in added:
            raise RuntimeError(f"No se encuentra el driver viostor en el ISO para {folder}: ISO de virtio-win incorrecto o incompleto")
        return added
    finally:
        ps(_dismount_script(image, iso))
