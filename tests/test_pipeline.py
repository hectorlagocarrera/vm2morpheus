import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vm2morpheus.models import DiskInfo, VMInfo
from vm2morpheus.morpheus import MorpheusClient, render_template
from vm2morpheus.pipeline import migrate
from vm2morpheus.sources.base import Source

pytestmark = pytest.mark.skipif(not shutil.which("qemu-img"), reason="qemu-img missing")


class FakeMorpheus(BaseHTTPRequestHandler):
    log = []
    uploads = {}

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        assert self.headers["Authorization"] == "Bearer tok"
        if self.path == "/api/virtual-images":
            self.log.append(("image", json.loads(body)))
            return self._send({"success": True, "virtualImage": {"id": 7, "status": "Saving"}})
        if self.path.startswith("/api/virtual-images/7/upload"):
            self.uploads[7] = body
            return self._send({"success": True})
        if self.path == "/api/instances":
            self.log.append(("instance", json.loads(body)))
            return self._send({"success": True, "instance": {"id": 42, "name": "vm1"}})
        self._send({"success": False, "msg": "nope"}, 404)

    def do_GET(self):
        if self.path == "/api/instances/5":
            return self._send({"success": True, "instance": {
                "cloud": {"id": 1}, "group": {"id": 2}, "instanceType": {"code": "hvm"}, "layout": {"id": 4},
                "plan": {"id": 6}, "config": {"imageId": 3, "hostId": 3, "memory": 3},
                "volumes": [{"name": "root", "rootVolume": True, "size": 20, "type": {"id": 9}}],
                "interfaces": [{"network": {"id": 11}}]}})
        if self.path == "/api/virtual-images/7":
            return self._send({"success": True, "virtualImage": {"id": 7, "status": "Active"}})
        self._send({"success": False, "msg": "nope"}, 404)


class FakeSource(Source):
    def __init__(self, disk):
        self.disk = disk
        self.off = False

    def connect(self): pass

    def list_vms(self): return []

    def get_vm(self, name):
        return VMInfo(name, 2, 2048, "uefi", "linux", "off" if self.off else "on",
                      [DiskInfo("d.vmdk", 8 << 20, "x")])

    def power_off(self, name): self.off = True

    def fetch_disks(self, vm, dest, progress):
        out = dest / "d.vmdk"
        shutil.copy(self.disk, out)
        return [out]


def test_end_to_end(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    mcfg = {"image_type": "qcow2", "instance": {"instance": {"name": "{vm_name}", "image": "{image_id}"},
                                                "config": {"memory": "{memory_bytes}"}}}
    res = migrate(FakeSource(disk), client, "vm1", {"workdir": str(tmp_path / "w"), "shutdown_source": True},
                  mcfg, log=lambda *_: None)
    srv.shutdown()
    assert res["instance"]["id"] == 42
    assert FakeMorpheus.uploads[7][:4] == b"QFI\xfb"  # qcow2 magic
    kinds = dict(FakeMorpheus.log)
    assert kinds["image"]["virtualImage"]["uefi"] is True
    assert kinds["instance"]["instance"] == {"name": "vm1", "image": 7}
    assert kinds["instance"]["config"]["memory"] == 2048 * 1024 * 1024


def test_render_template_types():
    assert render_template({"a": "{x}", "b": ["n-{x}"]}, {"x": 5}) == {"a": 5, "b": ["n-5"]}


def test_refuses_running_vm_without_shutdown(tmp_path):
    src = FakeSource(tmp_path / "x")
    with pytest.raises(RuntimeError, match="--shutdown"):
        migrate(src, MorpheusClient("http://x", "t"), "vm1", {"workdir": str(tmp_path)}, {}, log=lambda *_: None)


def test_gui_synthetic_reference_builds_hvm_payload():
    from vm2morpheus.gui import JobLog, synthetic_reference
    from vm2morpheus.morpheus import payload_from_reference
    f = {"cloud_id": "1", "group_id": "2", "plan_id": "3", "layout_id": "32", "network_id": "5",
         "resource_pool": "pool-9"}
    p = payload_from_reference(synthetic_reference(f), "vm1", None, 77, root_gb=110, extra_gb=[100], uefi=False)
    assert p["zoneId"] == 1 and p["instance"]["layout"] == {"id": 32}
    assert p["instance"]["instanceType"] == {"code": "mvm"}
    assert p["config"]["imageId"] == 77 and p["config"]["resourcePoolId"] == "pool-9"
    assert p["networkInterfaces"] == [{"network": {"id": "network-5"}}]
    assert [v["size"] for v in p["volumes"]] == [110, 100] and p["volumes"][0]["datastoreId"] == "auto"
    jl = JobLog()
    jl.write("a\n\rp 10%\rp 50%")
    assert jl.text() == "a\np 50%"


def test_gui_http_serves_page_and_rejects_unknown_job():
    import urllib.request
    from vm2morpheus.gui import Handler
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    page = urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/").read().decode()
    srv.shutdown()
    assert "vm2morpheus" in page and "Migrar" in page


def test_direct_source_disk_is_never_deleted(tmp_path):
    disk = tmp_path / "orig.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)

    class Direct(FakeSource):
        direct = True

        def fetch_disks(self, vm, dest, progress):
            return [disk]  # in place, outside the work folder

    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    migrate(Direct(disk), client, "vm1", {"workdir": str(tmp_path / "w"), "shutdown_source": True},
            {"image_type": "qcow2"}, log=lambda *_: None)
    srv.shutdown()
    assert disk.exists()
    assert FakeMorpheus.uploads[7][:4] == b"QFI\xfb"


def test_stale_state_is_discarded_when_image_missing_in_morpheus(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    wd = tmp_path / "w" / "vm1"
    wd.mkdir(parents=True)
    (wd / "state.json").write_text(json.dumps({"done": ["fetch", "convert", "drivers", "upload", "instance"],
                                               "images": [{"id": 99, "name": "vm1"}]}))
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    res = migrate(FakeSource(disk), client, "vm1", {"workdir": str(tmp_path / "w"), "shutdown_source": True},
                  {"image_type": "qcow2"}, log=lambda *_: None)
    srv.shutdown()
    assert res["images"] == [{"id": 7, "name": "vm1"}]


def test_instance_cloned_from_reference_swaps_only_image(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    migrate(FakeSource(disk), client, "vm1", {"workdir": str(tmp_path / "w2"), "shutdown_source": True},
            {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}},
            log=lambda *_: None)
    srv.shutdown()
    sent = dict(FakeMorpheus.log)["instance"]
    assert sent["config"] == {"imageId": 7, "hostId": 3, "memory": 3}
    assert sent["instance"]["layout"] == {"id": 4} and sent["zoneId"] == 1
    assert sent["networkInterfaces"] == [{"network": {"id": "11"}}]
    assert sent["volumes"][0]["size"] == 20 and sent["volumes"][0]["storageType"] == 9


def test_reference_payload_adds_blank_extra_volumes():
    from vm2morpheus.morpheus import payload_from_reference
    ref = {"cloud": {"id": 1}, "group": {"id": 2}, "instanceType": {"code": "hvm"}, "layout": {"id": 4},
           "plan": {"id": 6}, "config": {"imageId": 3},
           "volumes": [{"name": "root", "rootVolume": True, "size": 20, "type": {"id": 9}}]}
    p = payload_from_reference(ref, "vm1", 3, 7, root_gb=110, extra_gb=[100])
    assert p["volumes"][0]["size"] == 110
    assert p["volumes"][1] == {"id": -1, "name": "data1", "rootVolume": False, "size": 100, "storageType": 9}


def test_two_disks_are_bundled_with_metadata_json(tmp_path):
    import io
    import zipfile
    d0, d1 = tmp_path / "a.vmdk", tmp_path / "b.vmdk"
    for d, size in ((d0, "8M"), (d1, "4M")):
        subprocess.run(["qemu-img", "create", "-f", "vmdk", str(d), size], check=True, capture_output=True)

    class Two(FakeSource):
        def get_vm(self, name):
            return VMInfo(name, 2, 2048, "bios", "windows", "off",
                          [DiskInfo("a.vmdk", 8 << 20, "x"), DiskInfo("b.vmdk", 4 << 20, "y")])

        def fetch_disks(self, vm, dest, progress):
            out = []
            for src in (d0, d1):
                shutil.copy(src, dest / src.name)
                out.append(dest / src.name)
            return out

    FakeMorpheus.log.clear()
    FakeMorpheus.uploads.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    migrate(Two(d0), client, "vm1", {"workdir": str(tmp_path / "w3")}, {"image_type": "qcow2",
            "reference_instance": {"instance_id": 5, "image_id": 3}}, log=lambda *_: None)
    srv.shutdown()
    z = zipfile.ZipFile(io.BytesIO(FakeMorpheus.uploads[7]))
    assert sorted(z.namelist()) == ["metadata.json", "vm1-sda", "vm1-sdb"]
    meta = json.loads(z.read("metadata.json"))
    assert meta["disks"] == [
        {"capacity": 8 << 20, "guestDeviceName": "sda", "position": 0, "name": "root", "file": "vm1-sda"},
        {"capacity": 4 << 20, "guestDeviceName": "sdb", "position": 1, "name": "data", "file": "vm1-sdb"}]
    assert z.read("vm1-sda")[:4] == b"QFI\xfb" and z.read("vm1-sdb")[:4] == b"QFI\xfb"
    inst = dict(FakeMorpheus.log)["instance"]
    assert [(v["name"], v["rootVolume"]) for v in inst["volumes"]] == [("root", True), ("data", False)]


def test_reference_payload_autodetects_image_value():
    from vm2morpheus.morpheus import payload_from_reference
    ref = {"cloud": {"id": 1}, "group": {"id": 2}, "instanceType": {"code": "hvm"}, "layout": {"id": 4},
           "plan": {"id": 6}, "config": {"imageId": 3, "hostId": 3, "imageType": "qcow2"}}
    p = payload_from_reference(ref, "vm1", None, 7)
    assert p["config"] == {"imageId": 7, "hostId": 3, "imageType": "qcow2"}


def test_redact_hides_credentials():
    from vm2morpheus.gui import redact
    assert redact({"a": {"adminPassword": "x", "n": 1}, "l": [{"apiToken": "t"}]}) == {
        "a": {"adminPassword": "***", "n": 1}, "l": [{"apiToken": "***"}]}


def test_reference_like_real_hvm_instance():
    from vm2morpheus.morpheus import payload_from_reference
    ref = {"cloud": {"id": 1}, "group": {"id": 1}, "instanceType": {"code": "mvm"}, "layout": {"id": 32},
           "plan": {"id": 23},
           "config": {"imageId": None, "uefi": False, "resourcePoolId": "pool-1", "kvmHostId": None,
                      "backup": {"createBackup": False}},
           "volumes": [{"name": "root", "rootVolume": True, "size": 80, "storageType": 1, "datastoreId": "auto"}],
           "interfaces": [{"id": "network-1", "network": {"id": 1}}]}
    p = payload_from_reference(ref, "w2k16", None, 55, root_gb=110, extra_gb=[100], root_only=False, uefi=True)
    assert p["config"]["imageId"] == 55 and p["config"]["kvmHostId"] is None and p["config"]["uefi"] is True
    assert p["volumes"][0] == {"id": -1, "name": "root", "rootVolume": True, "size": 110,
                               "storageType": 1, "datastoreId": "auto"}
    assert p["volumes"][1]["storageType"] == 1 and p["networkInterfaces"] == [{"network": {"id": "network-1"}}]
    assert p["instance"]["instanceType"] == {"code": "mvm"}


def test_settings_drop_secrets_unless_opted_in(tmp_path, monkeypatch):
    from vm2morpheus import settings
    monkeypatch.setattr(settings, "PATH", tmp_path / "s" / "settings.json")
    form = {"source_host": "10.0.0.5", "source_password": "pw", "morpheus_token": "tok", "vm": "w2k16"}
    settings.save(form)
    assert settings.load() == {"source_host": "10.0.0.5", "vm": "w2k16"}
    settings.save({**form, "remember_secrets": True})
    assert settings.load()["morpheus_token"] == "tok"
    monkeypatch.setattr(settings, "PATH", tmp_path / "missing.json")
    assert settings.load() == {}


def test_hyperv_list_vms_handles_single_and_many_results():
    from vm2morpheus.sources.hyperv import HyperVSource
    h = HyperVSource({"host": "h", "username": "u", "password": "p"})
    h._ps = lambda script: json.dumps({"Name": "w2k16", "State": "Off", "Cpu": 1, "MemoryMB": 4024})
    assert h.list_vms() == [{"name": "w2k16", "state": "off", "cpu": 1, "memory_mb": 4024}]
    h._ps = lambda script: json.dumps([{"Name": "b", "State": "Running", "Cpu": 2, "MemoryMB": 1},
                                       {"Name": "A", "State": "Off", "Cpu": 1, "MemoryMB": 2}])
    assert [v["name"] for v in h.list_vms()] == ["A", "b"]
    h._ps = lambda script: ""
    assert h.list_vms() == []
