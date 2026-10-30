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
                raise MorpheusError(f"image {image_id} entered status {img.get('status')!r}")
            if time.monotonic() > deadline:
                raise MorpheusError(f"image {image_id} not ready after {timeout}s (status {status!r})")
            time.sleep(poll)

    # --- instances -------------------------------------------------------
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
