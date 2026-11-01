"""Writes the Windows version resource (Explorer > Properties > Details) used by the exe build.

Run:  python tools/make_versionfile.py build/version.txt"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def render(version: str) -> str:
    nums = (re.findall(r"\d+", version) + ["0"] * 4)[:4]
    tup = ", ".join(nums)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040A04B0', [
      StringStruct('CompanyName', 'Redes'),
      StringStruct('FileDescription', 'vm2morpheus - migracion de VMs a HPE Morpheus VM Essentials'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'vm2morpheus'),
      StringStruct('OriginalFilename', 'vm2morpheus.exe'),
      StringStruct('ProductName', 'vm2morpheus'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [1034, 1200])])
  ]
)
"""


if __name__ == "__main__":
    ns: dict = {}
    exec((ROOT / "vm2morpheus" / "__init__.py").read_text(), ns)
    out = Path(sys.argv[1])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(ns["__version__"]), encoding="utf-8")
