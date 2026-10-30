import json
import os
import time
from pathlib import Path

import requests


class MorpheusError(RuntimeError):
    pass


class _ProgressReader:
    """File wrapper with __len__ so requests sends a Content-Length instead of chunked encoding."""

    def __init__(self, path: Path, cb):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)
        self.done = 0
        self.cb = cb

    def __len__(self):
        return self.size

    def read(self, n=-1):
        data = self.f.read(n)
        self.done += len(data)
        if self.cb:
            self.cb(self.done, self.size)
        return data

    def close(self):
        self.f.close()


class MorpheusClient:
    def __init__(self, url: str, token: str, verify: bool = True):
        self.base = url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.s.verify = verify

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.s.request(method, f"{self.base}{path}", **kw)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code >= 400 or body.get("success") is False:
            raise MorpheusError(f"{method} {path} -> {r.status_code}: {body.get('msg') or r.text[:300]}"
                                f" {body.get('errors') or ''}".strip())
        return body

    # --- discovery -------------------------------------------------------
    def clouds(self) -> list[dict]:
        return self._req("GET", "/api/zones", params={"max": 100}).get("zones", [])

    def find_one(self, collection: str, key: str, name: str) -> dict | None:
        items = self._req("GET", f"/api/{collection}", params={"name": name, "max": 25}).get(key, [])
        return next((i for i in items if i.get("name") == name), None)

    # --- images ----------------------------------------------------------
    def create_virtual_image(self, name: str, image_type: str, os_type=None, uefi: bool = False,
                             storage_provider_id=None, extra: dict | None = None) -> dict:
        vi = {"name": name, "imageType": image_type, "uefi": uefi}
        if os_type is not None:
            vi["osType"] = os_type
        if storage_provider_id is not None:
            vi["storageProvider"] = {"id": storage_provider_id}
        vi.update(extra or {})
        return self._req("POST", "/api/virtual-images", json={"virtualImage": vi})["virtualImage"]

    def upload_image(self, image_id: int, path: Path, progress=None) -> None:
        reader = _ProgressReader(path, progress)
        try:
            self._req("POST", f"/api/virtual-images/{image_id}/upload", params={"filename": path.name},
                      data=reader, headers={"Content-Type": "application/octet-stream"}, timeout=(30, 3600))
        finally:
            reader.close()

    def delete_image(self, image_id: int) -> None:
        self._req("DELETE", f"/api/virtual-images/{image_id}")

    def image_exists(self, image_id: int) -> bool:
        try:
            self._req("GET", f"/api/virtual-images/{image_id}")
            return True
        except MorpheusError as e:
            if " 404:" in str(e):
                return False
            raise

    def wait_image_ready(self, image_id: int, timeout: int = 3600, poll: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            img = self._req("GET", f"/api/virtual-images/{image_id}")["virtualImage"]
            status = str(img.get("status", "")).lower()
            if status in ("active", "ready"):
                return img
            if status in ("failed", "error"):
                raise MorpheusError(f"La imagen {image_id} pasó al estado {img.get('status')!r}")
            if time.monotonic() > deadline:
                raise MorpheusError(f"La imagen {image_id} no está lista tras {timeout}s (estado {status!r})")
            time.sleep(poll)

    # --- instances -------------------------------------------------------
    def layouts(self, instance_type_code: str = "mvm") -> list[tuple[int, str]]:
        """Layouts of an instance type as (id, name). Tries the library endpoints, then falls back to the
        layouts used by existing instances of that type (a Morpheus without any of them returns [])."""
        found: dict[int, str] = {}
        try:
            types = self._req("GET", "/api/library/instance-types", params={"code": instance_type_code, "max": 50})
            for t in types.get("instanceTypes", []):
                if t.get("code") != instance_type_code:
                    continue
                for l in t.get("instanceTypeLayouts") or t.get("layouts") or []:
                    found[l["id"]] = l.get("name", "")
                if not found and t.get("id") is not None:
                    try:
                        sub = self._req("GET", f"/api/library/instance-types/{t['id']}/layouts", params={"max": 100})
                        for l in sub.get("instanceTypeLayouts") or sub.get("layouts") or []:
                            found[l["id"]] = l.get("name", "")
                    except MorpheusError:
                        pass
        except MorpheusError:
            pass
        if not found:
            try:
                for i in self._req("GET", "/api/instances", params={"max": 200}).get("instances", []):
                    if (i.get("instanceType") or {}).get("code") == instance_type_code and i.get("layout"):
                        found[i["layout"]["id"]] = i["layout"].get("name", "")
            except MorpheusError:
                pass
        return sorted(found.items(), key=lambda kv: kv[1].lower())

    def find_layout(self, instance_type_code: str = "mvm", prefer: str = "single") -> tuple[int, str]:
        options = self.layouts(instance_type_code)
        pick = next((o for o in options if prefer in o[1].lower()), options[0] if len(options) == 1 else None)
        if not pick:
            names = ", ".join(f"{n} (id {i})" for i, n in options) or "ninguno"
            raise MorpheusError(f"No se pudo elegir el layout de {instance_type_code} automáticamente "
                                f"(encontrados: {names}). Elígelo a mano en la interfaz.")
        return pick

    def resource_pools(self, cloud_id: int) -> list[tuple[str, str]]:
        try:
            data = self._req("GET", f"/api/zones/{cloud_id}/resource-pools", params={"max": 100})
        except MorpheusError:
            return []
        return [(f"pool-{p['id']}", p.get("name", "")) for p in data.get("resourcePools", []) if p.get("active", True)]

    def get_instance(self, instance_id: int) -> dict:
        return self._req("GET", f"/api/instances/{instance_id}")["instance"]

    def create_instance(self, payload: dict) -> dict:
        return self._req("POST", "/api/instances", json=payload)["instance"]


def render_template(obj, values: dict):
    """Substitute {placeholders} in a nested dict/list/str. A string that is exactly one
    placeholder keeps the value's type (so "{image_id}" becomes an int)."""
    if isinstance(obj, dict):
        return {k: render_template(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_template(v, values) for v in obj]
    if isinstance(obj, str):
        for k, v in values.items():
            if obj == "{" + k + "}":
                return v
        try:
            return obj.format(**values)
        except (KeyError, IndexError):
            return obj
    return obj


def payload_from_reference(ref: dict, name: str, old_image_id: int | None, new_image_id: int,
                           root_gb: int | None = None, extra_gb: list[int] | None = None,
                           root_only: bool = False, uefi: bool | None = None,
                           extra_name: str | None = None, boot_sata: bool = False) -> dict:
    """Build POST /api/instances from an instance created by hand in the UI, swapping its image.

    Only values under keys that mention "image" are swapped (so an unrelated number that happens to
    equal the image id is left alone). Raises if the reference image id is not found.
    """
    swapped = []

    def swap(o, in_image_key=False):
        if in_image_key and o is None and old_image_id is None:  # e.g. "imageId": null on a blank reference VM
            swapped.append(o)
            return new_image_id
        if isinstance(o, dict):
            return {k: swap(v, in_image_key or "image" in k.lower()) for k, v in o.items()}
        if isinstance(o, list):
            return [swap(v, in_image_key) for v in o]
        if in_image_key and not isinstance(o, bool) and (
                str(o) == str(old_image_id) if old_image_id is not None
                else isinstance(o, int) or (isinstance(o, str) and o.isdigit())):
            swapped.append(o)
            return type(o)(new_image_id)
        return o

    config = swap(ref.get("config") or {})
    if uefi is not None and "uefi" in config:
        config["uefi"] = uefi
    if not swapped:
        raise MorpheusError(f"No se encontró el id de imagen {old_image_id or '(cualquiera)'} en la configuración de la "
                            "instancia de referencia; revisa la instancia o el ID de la imagen")
    payload = {
        "zoneId": ref["cloud"]["id"],
        "instance": {
            "name": name,
            "site": {"id": ref["group"]["id"]},
            "instanceType": {"code": ref["instanceType"]["code"]},
            "layout": {"id": ref["layout"]["id"]},
            "plan": {"id": ref["plan"]["id"]},
        },
        "config": config,
    }
    volumes = []
    for v in ref.get("volumes") or []:
        if root_only and not v.get("rootVolume"):
            continue
        nv = {"id": -1, "name": v.get("name"), "rootVolume": bool(v.get("rootVolume")), "size": v.get("size")}
        st = v.get("storageType", v.get("type"))
        st = st.get("id") if isinstance(st, dict) else st
        if st is not None:
            nv["storageType"] = st
        if v.get("datastoreId") is not None:
            nv["datastoreId"] = v["datastoreId"]
        if nv["rootVolume"] and root_gb:
            nv["size"] = max(int(nv["size"] or 0), root_gb)  # the root disk must hold the migrated image
        volumes.append(nv)
    root = next((v for v in volumes if v["rootVolume"]), None)
    if boot_sata and root:
        root["controllerMountPoint"] = sata_mount_point(ref)
    for i, gb in enumerate(extra_gb or [], start=1):  # blank data disks sized like the source's extra disks
        nv = {"id": -1, "name": extra_name or f"data{i}", "rootVolume": False, "size": gb}
        for k in ("storageType", "datastoreId"):
            if root and k in root:
                nv[k] = root[k]
        volumes.append(nv)
    if volumes:
        payload["volumes"] = volumes
    nics = []
    for i in ref.get("interfaces") or []:
        if i.get("network"):
            nid = i["id"] if isinstance(i.get("id"), str) and i["id"].startswith("network") else str(i["network"]["id"])
            nics.append({"network": {"id": nid}})
    if nics:
        payload["networkInterfaces"] = nics
    return payload


def sata_mount_point(ref: dict) -> str:
    """controllerMountPoint ("controllerId:bus:controllerTypeId:unit", as seen on a real HVM volume) for the SATA
    controller. Uses the reference instance's SATA controller when it lists one; otherwise a placeholder id."""
    for c in ref.get("controllers") or []:
        if (c.get("type") or {}).get("code") == "kvm-sata":
            return f"{c['id']}:{c.get('busNumber', 0)}:{c['type']['id']}:0"
    return "-1:0:8:0"  # 8 = id of the "kvm-sata" controller type on the observed Morpheus; -1 = not created yet
