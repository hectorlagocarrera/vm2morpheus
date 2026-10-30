import json
import zipfile
from pathlib import Path

LETTERS = "abcdefghijklmnopqrstuvwxyz"


def disk_file_name(vm_name: str, index: int) -> str:
    return f"{vm_name}-sd{LETTERS[index]}"


def build_bundle(vm_name: str, disks: list[tuple[Path, int]], zip_path: Path) -> None:
    """Multi-disk Morpheus image: a zip with one qcow2 per disk plus metadata.json.

    The metadata layout mirrors a sample exported from Morpheus (disks[].capacity/guestDeviceName/
    position/name/file); entries are stored without extension, named exactly like "file".
    """
    meta = {"disks": []}
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for i, (path, capacity) in enumerate(disks):
            fname = disk_file_name(vm_name, i)
            z.write(path, arcname=fname)
            meta["disks"].append({
                "capacity": capacity,
                "guestDeviceName": f"sd{LETTERS[i]}",
                "position": i,
                "name": "root" if i == 0 else "data",
                "file": fname,
            })
        z.writestr("metadata.json", json.dumps(meta, indent=2))
