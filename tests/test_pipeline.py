import json
from pathlib import Path
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
    reject_sata = False
    no_library = False
    existing_name = None
    existing_image_name = None
    name_clash = False
    instance_posts = 0

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
            sent = json.loads(body)
            type(self).instance_posts += 1
            if self.name_clash:
                return self._send({"success": False, "msg": "Failed to create instance",
                                   "errors": {"name": "must be unique"}}, 400)
            if self.reject_sata and any("controllerMountPoint" in v for v in sent.get("volumes", [])):
                return self._send({"success": False, "msg": "bad controller"}, 400)
            self.log.append(("instance", sent))
            return self._send({"success": True, "instance": {"id": 42, "name": "vm1"}})
        self._send({"success": False, "msg": "nope"}, 404)

    refuse_delete = False
    deleted = []

    def do_DELETE(self):
        if self.path.startswith("/api/virtual-images/"):
            if self.refuse_delete:
                return self._send({"success": False, "msg": "Image is in use by instance 37"}, 400)
            type(self).deleted.append(self.path)
            return self._send({"success": True})
        self._send({"success": False, "msg": "nope"}, 404)

    def do_GET(self):
        if self.path.startswith("/api/library/instance-types"):
            if self.no_library:
                return self._send({"success": False, "msg": "nope"}, 404)
            if self.path.startswith("/api/library/instance-types/10/layouts"):
                return self._send({"success": True, "instanceTypeLayouts": [
                    {"id": 33, "name": "Cluster HVM"}, {"id": 32, "name": "Single HVM"}]})
            return self._send({"success": True, "instanceTypes": [{"id": 10, "code": "mvm"}]})
        if self.path.startswith("/api/zones/1/resource-pools"):
            return self._send({"success": True, "resourcePools": [{"id": 1, "name": "ClusterLab01", "active": True}]})
        if self.path.startswith("/api/service-plans/6"):
            return self._send({"success": True, "servicePlan": {"id": 6, "name": "1 CPU, 512MB Memory", "maxCores": 1,
                                                                "maxMemory": 536870912}})
        if self.path.startswith("/api/virtual-images?"):
            hit = [{"id": 5, "name": self.existing_image_name}] if self.existing_image_name else []
            return self._send({"success": True, "virtualImages": hit})
        if self.path.startswith("/api/instances?") and "name=" in self.path:
            hit = [{"id": 99, "name": self.existing_name, "status": "running"}] if self.existing_name else []
            return self._send({"success": True, "instances": hit})
        if self.path.startswith("/api/instances?"):
            return self._send({"success": True, "instances": [
                {"instanceType": {"code": "mvm"}, "layout": {"id": 32, "name": "Single HVM"}},
                {"instanceType": {"code": "ubuntu"}, "layout": {"id": 99, "name": "Other"}}]})
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


def _run_with_reference(tmp_path, name, opts_extra=None):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    msgs = []
    migrate(FakeSource(disk), client, "vm1", {"workdir": str(tmp_path / name), "shutdown_source": True,
                                              **(opts_extra or {})},
            {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}}, log=msgs.append)
    srv.shutdown()
    return dict(FakeMorpheus.log)["instance"], msgs


def test_boot_disk_requested_on_sata_by_default(tmp_path):
    FakeMorpheus.reject_sata = False
    inst, _ = _run_with_reference(tmp_path, "w5")
    assert inst["volumes"][0]["controllerMountPoint"] == "-1:0:8:0"


def test_sata_rejected_by_morpheus_retries_without_it(tmp_path):
    FakeMorpheus.reject_sata = True
    try:
        inst, msgs = _run_with_reference(tmp_path, "w6")
    finally:
        FakeMorpheus.reject_sata = False
    assert "controllerMountPoint" not in inst["volumes"][0]
    assert any("se reintenta sin SATA" in m for m in msgs)


def test_sata_can_be_turned_off(tmp_path):
    FakeMorpheus.reject_sata = False
    inst, _ = _run_with_reference(tmp_path, "w7", {"boot_sata": False})
    assert "controllerMountPoint" not in inst["volumes"][0]


def test_sata_mount_point_uses_reference_controller():
    from vm2morpheus.morpheus import sata_mount_point
    ref = {"controllers": [{"id": 172, "busNumber": "0", "type": {"id": 9, "code": "kvm-virtio-blk"}},
                           {"id": 173, "busNumber": "0", "type": {"id": 8, "code": "kvm-sata"}}]}
    assert sata_mount_point(ref) == "173:0:8:0"


def test_virtio_os_folder_mapping():
    from vm2morpheus.drivers_windows import os_folder
    assert os_folder(14393, "Server") == "2k16" and os_folder(17763, "Server") == "2k19"
    assert os_folder(20348, "Server") == "2k22" and os_folder(26100, "Server") == "2k25"
    assert os_folder(19045, "Client") == "w10" and os_folder(22631, "Client") == "w11"
    assert os_folder(7601, "Server") == "2k8R2" and os_folder(1234, "Server") is None


def _fake_iso(tmp_path, folder="2k16", drivers=("viostor", "NetKVM")):
    for d in drivers:
        (tmp_path / "iso" / d / folder / "amd64").mkdir(parents=True)
    return str(tmp_path / "iso")


def test_inject_runs_dism_per_driver_and_always_dismounts(tmp_path):
    from vm2morpheus.drivers_windows import inject_virtio_windows
    iso_root = _fake_iso(tmp_path)
    calls, dism_calls = [], []

    def ps(script):
        calls.append(script)
        return json.dumps({"win": "E:\\", "iso": iso_root, "build": 14393, "type": "Server"}) if len(calls) == 1 else ""

    added = inject_virtio_windows(tmp_path / "d.vhdx", "x.iso", log=lambda *_: None, ps=ps, dism=dism_calls.append)
    assert added == ["viostor", "NetKVM"] and len(dism_calls) == 2 and len(calls) == 2  # mount + dismount
    assert dism_calls[0][:2] == ["/Image:E:\\", "/Add-Driver"]


def test_inject_dismounts_even_when_driver_missing(tmp_path):
    from vm2morpheus.drivers_windows import inject_virtio_windows
    iso_root = _fake_iso(tmp_path, drivers=("NetKVM",))  # no viostor
    calls = []

    def ps(script):
        calls.append(script)
        return json.dumps({"win": "E:\\", "iso": iso_root, "build": 14393, "type": "Server"}) if len(calls) == 1 else ""

    with pytest.raises(RuntimeError, match="viostor"):
        inject_virtio_windows(tmp_path / "d.vhdx", "x.iso", log=lambda *_: None, ps=ps, dism=lambda a: None)
    assert len(calls) == 2


def test_pipeline_injects_into_a_private_vhdx_copy_before_converting(tmp_path, monkeypatch):
    from vm2morpheus import pipeline
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    seen = {}

    def fake_inject(path, iso, log):
        seen["path"], seen["iso"] = path, iso
        assert path.suffix == ".vhdx" and path.exists()
        return ["viostor"]

    monkeypatch.setattr(pipeline, "_is_windows", lambda: True)
    monkeypatch.setattr(pipeline, "is_admin", lambda: True)
    monkeypatch.setattr(pipeline, "inject_virtio_windows", fake_inject)
    monkeypatch.setattr(pipeline, "ensure_virtio_iso", lambda p, progress=None: p or "cached.iso")

    class Win(FakeSource):
        direct = True

        def get_vm(self, name):
            vm = super().get_vm(name)
            vm.os_family = "windows"
            return vm

    FakeMorpheus.log.clear()
    FakeMorpheus.uploads.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    src = Win(disk)
    pipeline.migrate(src, client, "vm1", {"workdir": str(tmp_path / "w8"), "shutdown_source": True,
                                          "inject_drivers": True, "virtio_iso": "my.iso"},
                     {"image_type": "qcow2"}, log=lambda *_: None)
    srv.shutdown()
    assert src.direct is False and seen["iso"] == "my.iso"
    assert str(seen["path"]).startswith(str(tmp_path / "w8"))  # never the original disk
    assert FakeMorpheus.uploads[7][:4] == b"QFI\xfb"


def test_progress_raises_when_cancelled():
    import io
    from vm2morpheus.util import CancelledError, Progress
    ev = threading.Event()
    bar = Progress("x", io.StringIO(), cancel=ev)
    bar(1, 10)
    ev.set()
    with pytest.raises(CancelledError):
        bar(2, 10)


def test_convert_kills_qemu_img_on_cancel(tmp_path, monkeypatch):
    import time
    from vm2morpheus import convert
    from vm2morpheus.util import CancelledError
    fake = tmp_path / "fake-qemu"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setattr(convert, "require_tool", lambda name: str(fake))
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.monotonic()
    with pytest.raises(CancelledError):
        convert.convert_disk(tmp_path / "a", tmp_path / "b.qcow2", "qcow2", None, ev)
    assert time.monotonic() - t0 < 10


def test_cancel_between_steps_uploads_nothing(tmp_path):
    from vm2morpheus.util import CancelledError
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    ev = threading.Event()

    class Cancelling(FakeSource):
        def fetch_disks(self, vm, dest, progress):
            out = super().fetch_disks(vm, dest, progress)
            ev.set()  # the user presses Cancel right after the download
            return out

    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    with pytest.raises(CancelledError):
        migrate(Cancelling(disk), client, "vm1", {"workdir": str(tmp_path / "w9"), "shutdown_source": True},
                {"image_type": "qcow2"}, log=lambda *_: None, cancel=ev)
    srv.shutdown()
    assert FakeMorpheus.log == []  # no image created


def test_cancel_during_upload_removes_the_partial_image(tmp_path):
    from vm2morpheus.util import CancelledError
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    ev = threading.Event()
    deleted = []

    class Client(MorpheusClient):
        def upload_image(self, image_id, path, progress=None):
            ev.set()
            progress(1, 2)  # raises CancelledError

        def delete_image(self, image_id):
            deleted.append(image_id)

    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    with pytest.raises(CancelledError):
        migrate(FakeSource(disk), Client(f"http://127.0.0.1:{srv.server_port}", "tok"), "vm1",
                {"workdir": str(tmp_path / "w10"), "shutdown_source": True}, {"image_type": "qcow2"},
                log=lambda *_: None, cancel=ev)
    srv.shutdown()
    assert deleted == [7]


def test_pick_workdir_and_auto_workdir_is_remembered(tmp_path, monkeypatch):
    from vm2morpheus import gui, settings
    from vm2morpheus.util import pick_workdir
    assert pick_workdir().endswith("vm2morpheus-work")
    monkeypatch.setattr(settings, "PATH", tmp_path / "s.json")
    first = gui.resolve_workdir({"workdir": ""})
    settings.save({"vm": "x"})  # the form is saved on every action; the auto folder must survive it
    assert gui.resolve_workdir({"workdir": ""}) == first
    assert gui.resolve_workdir({"workdir": "D:\\\\mine"}) == "D:\\\\mine"


def test_driver_injection_without_admin_fails_before_any_work(tmp_path, monkeypatch):
    from vm2morpheus import pipeline
    monkeypatch.setattr(pipeline, "_is_windows", lambda: True)
    monkeypatch.setattr(pipeline, "is_admin", lambda: False)

    class Win(FakeSource):
        def get_vm(self, name):
            vm = super().get_vm(name)
            vm.os_family, vm.power_state = "windows", "off"
            return vm

    with pytest.raises(RuntimeError, match="administrador"):
        migrate(Win(tmp_path / "x"), MorpheusClient("http://x", "t"), "vm1",
                {"workdir": str(tmp_path / "w11"), "inject_drivers": True}, {}, log=lambda *_: None)
    assert not (tmp_path / "w11" / "vm1").exists()  # nothing was created or downloaded


def test_resume_and_dryrun_messages_are_readable(tmp_path):
    from vm2morpheus.pipeline import labels, relevant_steps
    assert relevant_steps({}, {}) == ["fetch", "convert", "upload"]
    assert relevant_steps({"inject_drivers": True}, {"reference_instance": {"instance_id": 5}}) == [
        "fetch", "convert", "drivers", "upload", "instance"]
    assert labels(["fetch", "upload"]) == "descarga de discos, subida a Morpheus"

    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    wd = tmp_path / "w12" / "vm1"
    wd.mkdir(parents=True)
    ready = wd / "vm1.qcow2"
    subprocess.run(["qemu-img", "create", "-f", "qcow2", str(ready), "8M"], check=True, capture_output=True)
    (wd / "state.json").write_text(json.dumps({"done": ["fetch", "convert"], "fetched": [str(disk)],
                                               "converted": [str(ready)]}))
    msgs = []
    with pytest.raises(Exception):  # no Morpheus is listening: the run stops at the upload, after the message
        migrate(FakeSource(disk), MorpheusClient("http://127.0.0.1:1", "t"), "vm1",
                {"workdir": str(tmp_path / "w12"), "shutdown_source": True}, {"image_type": "qcow2"}, log=msgs.append)
    text = "\n".join(msgs)
    assert "Ya hecho: descarga de discos, conversión a qcow2. Falta: subida a Morpheus." in text
    assert "fetch" not in text and "drivers" not in text


def test_tracker_weights_and_transitions():
    from vm2morpheus.tracker import Tracker
    events = []
    t = Tracker(["fetch", "convert", "upload"], ["fetch"], events.append)
    assert events[0]["overall"] == pytest.approx(0.30 / 0.85) and events[0]["step"] is None
    t.begin("convert")
    t.update("convert", 0.5)
    assert events[-1]["index"] == 2 and events[-1]["total_steps"] == 3
    assert events[-1]["overall"] == pytest.approx((0.30 + 0.25 * 0.5) / 0.85)
    t.update("upload", 0.9)  # not the current step: ignored
    assert events[-1]["fraction"] == 0.5
    t.finish("convert")
    t.complete()
    assert events[-1]["overall"] == 1.0 and events[-1]["label"] == ""


def test_pipeline_reports_monotonic_progress_ending_at_100(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client = MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")
    events = []
    migrate(FakeSource(disk), client, "vm1", {"workdir": str(tmp_path / "w13"), "shutdown_source": True},
            {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}},
            log=lambda *_: None, on_progress=events.append)
    srv.shutdown()
    overall = [e["overall"] for e in events]
    assert overall == sorted(overall) and overall[-1] == 1.0
    assert [s for s in ("fetch", "convert", "upload", "instance") if any(e["step"] == s for e in events)] == [
        "fetch", "convert", "upload", "instance"]


def test_browse_lists_folders_with_free_space(tmp_path):
    from vm2morpheus.util import browse
    (tmp_path / "Beta").mkdir()
    (tmp_path / "alfa").mkdir()
    (tmp_path / "$Recycle").mkdir()
    (tmp_path / "file.txt").write_text("x")
    r = browse(str(tmp_path))
    assert r["dirs"] == ["alfa", "Beta"] and r["free"] > 0 and r["parent"] == str(tmp_path.parent)
    assert browse("")["drives"] and browse("")["path"] == ""
    with pytest.raises(ValueError, match="no existe"):
        browse(str(tmp_path / "nope"))


def test_browse_endpoint_over_http(tmp_path):
    import urllib.parse
    import urllib.request
    from vm2morpheus.gui import Handler
    (tmp_path / "datos").mkdir()
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/browse?path=" + urllib.parse.quote(str(tmp_path))
    data = json.loads(urllib.request.urlopen(url).read())
    srv.shutdown()
    assert data["dirs"] == ["datos"]


def _client(srv):
    return MorpheusClient(f"http://127.0.0.1:{srv.server_port}", "tok")


def test_layout_and_pool_detection(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = _client(srv)
    assert c.layouts("mvm") == [(33, "Cluster HVM"), (32, "Single HVM")]
    assert c.find_layout("mvm") == (32, "Single HVM")
    assert c.resource_pools(1) == [("pool-1", "ClusterLab01")]
    FakeMorpheus.no_library = True  # older/limited API: fall back to the layouts of existing HVM instances
    try:
        assert c.layouts("mvm") == [(32, "Single HVM")]
        assert c.find_layout("mvm") == (32, "Single HVM")
    finally:
        FakeMorpheus.no_library = False
        srv.shutdown()


def test_ambiguous_layout_is_an_error_not_a_guess():
    from vm2morpheus.morpheus import MorpheusClient, MorpheusError
    c = MorpheusClient("http://x", "t")
    c.layouts = lambda code: [(1, "Cluster A"), (2, "Cluster B")]
    with pytest.raises(MorpheusError, match="Elígelo a mano"):
        c.find_layout("mvm")


def test_pipeline_detects_layout_and_pool_when_left_automatic(tmp_path):
    from vm2morpheus.gui import synthetic_reference
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ref = synthetic_reference({"cloud_id": "1", "group_id": "1", "plan_id": "23"})  # no layout, no pool
    msgs = []
    migrate(FakeSource(disk), _client(srv), "vm1", {"workdir": str(tmp_path / "w14"), "shutdown_source": True},
            {"image_type": "qcow2", "reference_instance": {"synthetic": ref}}, log=msgs.append)
    srv.shutdown()
    sent = dict(FakeMorpheus.log)["instance"]
    assert sent["instance"]["layout"] == {"id": 32} and sent["config"]["resourcePoolId"] == "pool-1"
    assert any("Layout detectado automáticamente: Single HVM (id 32)" in m for m in msgs)


def test_vmware_nfc_url_host_depends_on_vcenter_or_esxi():
    from vm2morpheus.sources.vmware import lease_url
    url = "https://*/nfc/52d1/disk-0.vmdk"
    assert lease_url(url, "VirtualCenter", "esxi01.lab", "vcenter.lab") == "https://esxi01.lab/nfc/52d1/disk-0.vmdk"
    assert lease_url(url, "HostAgent", "esxi01.lab", "esxi01.lab") == "https://esxi01.lab/nfc/52d1/disk-0.vmdk"
    assert lease_url(url, "VirtualCenter", "", "vcenter.lab") == "https://vcenter.lab/nfc/52d1/disk-0.vmdk"


def test_esxi_alias_and_license_hint():
    from vm2morpheus.sources import make_source
    from vm2morpheus.sources.vmware import VMwareSource, explain_error
    src = make_source({"type": "esxi", "host": "10.0.0.9", "username": "root", "password": "p"})
    assert isinstance(src, VMwareSource)

    class RestrictedVersion(Exception):
        pass

    msg = str(explain_error(RestrictedVersion("Current license or ESXi version prohibits execution")))
    assert "solo lectura" in msg and "vCenter" in msg
    other = ValueError("boom")
    assert explain_error(other) is other


def test_no_window_flag_only_on_windows():
    from vm2morpheus.util import no_window
    assert no_window("win32") == {"creationflags": 0x08000000} and no_window("linux") == {}


def test_launcher_logs_to_file_when_there_is_no_console(tmp_path, monkeypatch):
    import sys
    from vm2morpheus import launcher
    monkeypatch.setattr(launcher, "LOG", tmp_path / "logs" / "x.log")
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    handle = launcher.setup_streams()
    try:
        assert sys.stderr is handle and sys.stdout is handle
        print("hola", file=sys.stderr)
    finally:
        handle.close()
    assert "hola" in (tmp_path / "logs" / "x.log").read_text(encoding="utf-8")


def test_launcher_reports_unexpected_errors(monkeypatch):
    from vm2morpheus import launcher
    shown = []
    monkeypatch.setattr(launcher, "show_error", shown.append)

    def boom():
        raise ValueError("kaput")

    assert launcher.run(boom, show_box=True) == 1 and "kaput" in shown[0]

    def bye():
        raise SystemExit("mensaje de salida")

    assert launcher.run(bye, show_box=True) == 1 and shown[-1] == "mensaje de salida"
    assert launcher.run(lambda: 0) == 0


def test_idle_watchdog_rule():
    from vm2morpheus.gui import idle_too_long
    assert not idle_too_long(None, 1000, False)          # the page never connected: keep running
    assert not idle_too_long(900, 1000, False)           # recent heartbeat
    assert idle_too_long(100, 1000, False)               # tab closed long ago
    assert not idle_too_long(100, 1000, True)            # ...but never while a migration runs


def test_ping_and_shutdown_endpoints(tmp_path):
    import urllib.request
    from vm2morpheus import gui
    srv = HTTPServer(("127.0.0.1", 0), gui.Handler)
    gui.STATE["server"] = srv
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_port}"
    urllib.request.urlopen(base + "/ping").read()
    assert gui.STATE["last_ping"] is not None
    urllib.request.urlopen(urllib.request.Request(base + "/shutdown", data=b"{}", method="POST")).read()
    t.join(timeout=5)
    assert not t.is_alive()


def test_second_instance_reuses_the_running_one(capsys):
    import socket
    from vm2morpheus import gui
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        gui.serve(port, open_browser=False)  # must return instead of raising "address in use"
    finally:
        sock.close()
    assert "Ya hay una instancia" in capsys.readouterr().out


def test_only_selected_disks_are_migrated(tmp_path):
    import io
    import zipfile
    d0, d1, d2 = (tmp_path / n for n in ("a.vmdk", "b.vmdk", "c.vmdk"))
    for d in (d0, d1, d2):
        subprocess.run(["qemu-img", "create", "-f", "vmdk", str(d), "4M"], check=True, capture_output=True)

    class Three(FakeSource):
        def get_vm(self, name):
            return VMInfo(name, 2, 2048, "bios", "windows", "off",
                          [DiskInfo(f"{c}.vmdk", 4 << 20, "x", index=i) for i, c in enumerate("abc")])

        def fetch_disks(self, vm, dest, progress):
            out = []
            for d in vm.disks:  # only what the pipeline kept
                src = (d0, d1, d2)[d.index]
                shutil.copy(src, dest / src.name)
                out.append(dest / src.name)
            return out

    FakeMorpheus.log.clear()
    FakeMorpheus.uploads.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    msgs = []
    migrate(Three(d0), _client(srv), "vm1", {"workdir": str(tmp_path / "w15"), "shutdown_source": True,
                                             "disk_indexes": [0, 2]}, {"image_type": "qcow2"}, log=msgs.append)
    srv.shutdown()
    z = zipfile.ZipFile(io.BytesIO(FakeMorpheus.uploads[7]))
    meta = json.loads(z.read("metadata.json"))
    assert [d["capacity"] for d in meta["disks"]] == [4 << 20, 4 << 20] and len(meta["disks"]) == 2
    assert any("Discos seleccionados: 2 de 3" in m for m in msgs)
    with pytest.raises(RuntimeError, match="ningún disco"):
        migrate(Three(d0), _client(HTTPServer(("127.0.0.1", 0), FakeMorpheus)), "vm1",
                {"workdir": str(tmp_path / "w16"), "disk_indexes": []}, {}, log=lambda *_: None)


def test_registry_key_paths_have_single_backslashes():
    import re
    from vm2morpheus import window
    src = open(window.__file__, encoding="utf-8").read()
    assert "\\\\\\\\" not in re.sub(r'sep = "\\\\\\\\"', "", src)  # no doubled backslash left in a registry path
    assert window.webview2_installed() is False  # not Windows here


def test_serve_embedded_returns_when_the_window_closes(monkeypatch):
    import socket
    from vm2morpheus import gui, window
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    seen = {}

    def fake_window(url, busy, title="vm2morpheus"):
        seen["url"], seen["embedded_flag"] = url, gui.STATE["embedded"]
        return True  # the user opened and then closed the window

    monkeypatch.setattr(window, "open_window", fake_window)
    gui.serve(port, open_browser=True, embedded=True)
    assert seen["url"].endswith(str(port)) and seen["embedded_flag"] is True


def test_serve_falls_back_to_the_browser_without_a_window(monkeypatch, capsys):
    import socket
    from vm2morpheus import gui, window
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    opened = []

    def fake_browser(url):
        opened.append(url)
        threading.Timer(0.3, gui.STATE["server"].shutdown).start()  # end the loop so the test can finish

    monkeypatch.setattr(window, "open_window", lambda url, busy, title="x": False)
    monkeypatch.setattr(gui.webbrowser, "open", fake_browser)
    gui.serve(port, open_browser=True, embedded=True)
    assert opened and gui.STATE["embedded"] is False
    assert "se usa el navegador" in capsys.readouterr().out


def test_page_hides_the_quit_button_only_when_embedded():
    from vm2morpheus import gui
    gui.STATE["embedded"] = True
    assert "const EMBEDDED=true;" in gui.render_page()
    gui.STATE["embedded"] = False
    assert "const EMBEDDED=false;" in gui.render_page() and "__EMBEDDED__" not in gui.render_page()


def test_smb_session_uses_the_win32_api_once_per_share(monkeypatch):
    from pathlib import Path
    from vm2morpheus import winnet
    from vm2morpheus.sources.hyperv import HyperVSource
    calls, closed = [], []
    monkeypatch.setattr(winnet, "add_connection", lambda share, user, pw: calls.append((share, user, pw)))
    monkeypatch.setattr(winnet, "cancel_connection", closed.append)
    h = HyperVSource({"host": "10.0.0.5", "username": "u", "password": "p"})
    h.direct, h.local = True, False
    h.ensure_access([Path(r"\\10.0.0.5\D$\VMs\a.vhdx"), Path(r"\\10.0.0.5\D$\VMs\b.vhdx")])
    assert calls == [(r"\\10.0.0.5\D$", "u", "p")]  # no command line, one session for both disks
    h.close()
    assert closed == [r"\\10.0.0.5\D$"]


def test_powershell_is_not_started_with_execution_policy_bypass():
    import inspect
    from vm2morpheus import drivers_windows
    assert "Bypass" not in inspect.getsource(drivers_windows.run_powershell)


def test_make_mountable_clears_the_sparse_flag(tmp_path):
    from vm2morpheus.drivers_windows import SPARSE, make_mountable
    f = tmp_path / "d.vhdx"
    f.write_bytes(b"data")
    state, ran = {"flags": SPARSE}, []

    def run(cmd, **kw):
        ran.append(cmd[:3])
        state["flags"] = 0  # fsutil worked
    make_mountable(f, log=lambda *_: None, attrs=lambda p: state["flags"], run=run)
    assert ran == [["fsutil", "sparse", "setflag"]] and f.read_bytes() == b"data"


def test_make_mountable_rewrites_the_file_when_holes_remain(tmp_path):
    from vm2morpheus.drivers_windows import COMPRESSED, SPARSE, make_mountable
    f = tmp_path / "d.vhdx"
    f.write_bytes(b"abc" * 1000)
    calls = {"n": 0}

    def attrs(p):  # sparse until the file has been rewritten (the .dense temp name is gone again)
        calls["n"] += 1
        return SPARSE | COMPRESSED if calls["n"] <= 2 else 0

    ran = []
    make_mountable(f, log=lambda *_: None, attrs=attrs, run=lambda cmd, **kw: ran.append(cmd[0]))
    assert f.read_bytes() == b"abc" * 1000 and not (tmp_path / "d.vhdx.dense").exists()
    assert "compact" in ran and "fsutil" in ran


def test_make_mountable_gives_up_with_a_clear_message(tmp_path):
    from vm2morpheus.drivers_windows import SPARSE, make_mountable
    f = tmp_path / "d.vhdx"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="NTFS sin compresión"):
        make_mountable(f, log=lambda *_: None, attrs=lambda p: SPARSE, run=lambda cmd, **kw: None)


def test_failed_injection_keeps_the_converted_vhdx_in_the_saved_state(tmp_path, monkeypatch):
    from vm2morpheus import pipeline
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    monkeypatch.setattr(pipeline, "_is_windows", lambda: True)
    monkeypatch.setattr(pipeline, "is_admin", lambda: True)
    monkeypatch.setattr(pipeline, "ensure_virtio_iso", lambda p, progress=None: "x.iso")

    def boom(path, iso, log):
        raise RuntimeError("Mount-DiskImage falló")

    monkeypatch.setattr(pipeline, "inject_virtio_windows", boom)

    class Win(FakeSource):
        def get_vm(self, name):
            vm = super().get_vm(name)
            vm.os_family = "windows"
            return vm

    wd = tmp_path / "w17"
    with pytest.raises(RuntimeError, match="Mount-DiskImage"):
        pipeline.migrate(Win(disk), MorpheusClient("http://127.0.0.1:1", "t"), "vm1",
                         {"workdir": str(wd), "shutdown_source": True, "inject_drivers": True}, {},
                         log=lambda *_: None)
    saved = json.loads((wd / "vm1" / "state.json").read_text())
    assert saved["fetched"][0].endswith(".vhdx") and Path(saved["fetched"][0]).exists()


def test_describe_file_reports_the_attributes_that_matter(tmp_path):
    from vm2morpheus.drivers_windows import COMPRESSED, SPARSE, describe_file
    f = tmp_path / "d.vhdx"
    f.write_bytes(b"x")
    text = describe_file(f, attrs=lambda p: SPARSE | COMPRESSED)
    assert "disperso: sí" in text and "comprimido: sí" in text and "cifrado: no" in text
    assert describe_file(f, attrs=lambda p: 0).count("no") == 3


def _mount_ps(fail_mounts: int, iso_root=""):
    """Fake PowerShell: the first `fail_mounts` mount scripts fail with 0xc03a001a, dismounts always work."""
    state = {"mounts": 0, "dismounts": 0}

    def ps(script):
        if "Dismount" in script:
            state["dismounts"] += 1
            return ""
        state["mounts"] += 1
        if state["mounts"] <= fail_mounts:
            raise RuntimeError("Falló PowerShell: HRESULT 0xc03a001a,Mount-DiskImage")
        return json.dumps({"win": "E:\\", "iso": iso_root, "build": 14393, "type": "Server"})
    return ps, state


def test_mount_limitation_is_retried_after_rewriting_the_file(tmp_path):
    from vm2morpheus.drivers_windows import inject_virtio_windows
    disk = tmp_path / "d.vhdx"
    disk.write_bytes(b"vhdx-bytes" * 100)
    iso_root = _fake_iso(tmp_path)
    ps, state = _mount_ps(1, iso_root)
    msgs, dism_calls = [], []
    added = inject_virtio_windows(disk, "x.iso", log=msgs.append, ps=ps, dism=dism_calls.append)
    assert added == ["viostor", "NetKVM"] and state["mounts"] == 2
    assert any("se reintenta" in m for m in msgs)
    assert disk.read_bytes() == b"vhdx-bytes" * 100 and not (tmp_path / "d.vhdx.dense").exists()
    assert state["dismounts"] == 2  # one before the retry, one at the end


def test_mount_limitation_that_persists_is_explained_with_the_file_state(tmp_path):
    from vm2morpheus.drivers_windows import inject_virtio_windows
    disk = tmp_path / "d.vhdx"
    disk.write_bytes(b"x")
    ps, state = _mount_ps(99)
    with pytest.raises(RuntimeError, match="NTFS, sin comprimir"):
        inject_virtio_windows(disk, "x.iso", log=lambda *_: None, ps=ps, dism=lambda a: None)
    assert state["mounts"] == 2 and state["dismounts"] == 2  # tried twice, always dismounted


def test_no_undefined_names_anywhere_in_the_package():
    """Windows-only code paths never run on the CI/Linux box, so a missing import there (this happened with
    ctypes) would only show up on a user's machine. pyflakes finds those statically."""
    pyflakes_api = pytest.importorskip("pyflakes.api")
    import io
    from pathlib import Path as P
    import vm2morpheus
    out = io.StringIO()
    reporter = __import__("pyflakes.reporter", fromlist=["Reporter"]).Reporter(out, out)
    for f in P(vm2morpheus.__file__).parent.rglob("*.py"):
        pyflakes_api.check(f.read_text(encoding="utf-8"), str(f), reporter)
    undefined = [l for l in out.getvalue().splitlines() if "undefined name" in l]
    assert undefined == []


def test_default_is_the_browser_and_window_is_opt_in():
    import inspect
    from vm2morpheus import gui
    from vm2morpheus.cli import build_parser
    assert inspect.signature(gui.serve).parameters["embedded"].default is False
    assert build_parser().parse_args(["gui"]).ventana is False
    assert build_parser().parse_args(["gui", "--ventana"]).ventana is True


def test_no_message_box_when_there_is_a_console(monkeypatch):
    from vm2morpheus import launcher
    shown = []
    monkeypatch.setattr(launcher, "show_error", shown.append)

    def boom():
        raise ValueError("kaput")

    assert launcher.run(boom, show_box=False) == 1 and shown == []  # the traceback goes to the console instead


def test_the_windows_build_keeps_the_console_and_skips_pywebview():
    wf = open(".github/workflows/build-exe.yml", encoding="utf-8").read()
    assert "--noconsole" not in wf and "--collect-all webview" not in wf and "pythonnet" not in wf


def test_unverified_tls_notice_is_in_spanish_and_silences_urllib3():
    import io
    import warnings
    from vm2morpheus.gui import source_cfg
    from vm2morpheus.sources.vmware import note_unverified_tls
    out = io.StringIO()
    assert note_unverified_tls(False, "esxi1.lab", out) is True
    assert "no se verifica el certificado TLS de esxi1.lab" in out.getvalue()
    assert note_unverified_tls(True, "esxi1.lab", out) is False  # verifying: nothing to announce
    import urllib3
    assert any(f[0] == "ignore" and f[2] is urllib3.exceptions.InsecureRequestWarning for f in warnings.filters)
    base = {"source_type": "esxi", "source_host": "h", "source_user": "u", "source_password": "p"}
    assert source_cfg(dict(base))["verify_ssl"] is False
    assert source_cfg({**base, "verify_source": True})["verify_ssl"] is True


def test_lease_heartbeat_pings_from_a_thread_and_a_hung_ping_never_blocks_the_caller():
    import time as _t
    from vm2morpheus.sources.vmware import LeaseHeartbeat

    class Lease:
        def __init__(self):
            self.pings = []

        def HttpNfcLeaseProgress(self, pct):
            self.pings.append(pct)

    lease = Lease()
    hb = LeaseHeartbeat(lease, interval=0.05).start()
    hb.percent = 42
    _t.sleep(0.3)
    hb.stop()
    assert len(lease.pings) >= 2 and set(lease.pings) <= {0, 42}

    class Hung:
        def HttpNfcLeaseProgress(self, pct):
            threading.Event().wait()  # the ESXi never answers this SOAP call

    t0 = _t.monotonic()
    hb2 = LeaseHeartbeat(Hung(), interval=0.01).start()
    _t.sleep(0.1)
    hb2.stop()
    assert _t.monotonic() - t0 < 2  # the caller was never held up by the hung ping


def test_progress_shows_the_transfer_speed(monkeypatch):
    import io
    from vm2morpheus import util
    clock = iter([10.0, 20.0])
    monkeypatch.setattr(util.time, "monotonic", lambda: next(clock))
    out = io.StringIO()
    bar = util.Progress("descargar x", out)
    bar(0)
    bar(200 * 1024 * 1024)  # 200 MiB in 10 s
    assert "20.0 MiB/s" in out.getvalue()


def test_slow_actions_show_a_busy_indicator():
    from vm2morpheus import gui
    page = gui.render_page()
    assert 'id="busywrap"' in page and page.count("data-busy onclick") == 5
    for call in ('busy("Conectando con el origen', 'busy("Analizando la VM', 'busy("Cargando datos de Morpheus',
                 'busy("Leyendo la instancia de referencia'):
        assert call in page


class _FakeResponse:
    def __init__(self, chunks, stall=False):
        self.chunks, self.stall = chunks, stall

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, size):
        import requests
        yield from self.chunks
        if self.stall:
            raise requests.exceptions.ReadTimeout("no bytes for 120 s")


def test_download_to_writes_the_stream_and_pings_per_chunk(tmp_path):
    from vm2morpheus.sources.vmware import download_to
    seen, ticks = [], []
    size = download_to("https://esxi/x", tmp_path / "d.vmdk", "c=1", False, lambda d, t: seen.append(d),
                       lambda: ticks.append(1), getter=lambda *a, **k: _FakeResponse([b"aaaa", b"bb"]))
    assert size == 6 and (tmp_path / "d.vmdk").read_bytes() == b"aaaabb" and seen == [4, 6] and len(ticks) == 2


def test_stalled_download_fails_with_a_clear_message_instead_of_hanging(tmp_path):
    from vm2morpheus.sources.vmware import download_to
    with pytest.raises(RuntimeError, match="dejó de enviar datos durante 120 s"):
        download_to("https://esxi/x", tmp_path / "d.vmdk", "c=1", False, lambda d, t: None,
                    getter=lambda *a, **k: _FakeResponse([b"x" * 10], stall=True))


def test_download_asks_requests_for_a_read_timeout(tmp_path):
    from vm2morpheus.sources.vmware import READ_TIMEOUT, download_to
    captured = {}

    def getter(url, **kw):
        captured.update(kw)
        return _FakeResponse([b"x"])

    download_to("https://esxi/x", tmp_path / "d.vmdk", "c=1", False, lambda d, t: None, getter=getter)
    assert captured["timeout"] == (15, READ_TIMEOUT) and captured["stream"] is True and captured["verify"] is False


def test_progress_finish_reports_total_and_speed(monkeypatch):
    import io
    from vm2morpheus import util
    clock = iter([5.0, 15.0, 15.0])
    monkeypatch.setattr(util.time, "monotonic", lambda: next(clock))
    out = io.StringIO()
    bar = util.Progress("descargar x", out)
    bar(0)
    bar(300 * 1024 * 1024)
    bar.finish()
    last = out.getvalue().splitlines()[-1]
    assert "300.0 MiB" in last and "30.0 MiB/s" in last and "terminado en 10 s" in last


def test_format_duration():
    from vm2morpheus.util import format_duration
    assert format_duration(45) == "45 s" and format_duration(150) == "2 min 30 s"
    assert format_duration(120) == "2 min" and format_duration(3900) == "1 h 05 min"


def _timed_progress(monkeypatch, ticks, **kw):
    import io
    from vm2morpheus import util
    clock = iter(ticks)
    monkeypatch.setattr(util.time, "monotonic", lambda: next(clock))
    out = io.StringIO()
    return util.Progress("descargar x", out, **kw), out


def test_eta_with_a_known_total(monkeypatch):
    bar, out = _timed_progress(monkeypatch, [0.0, 10.0])
    bar(0, 1000)
    bar(100, 1000)  # 10 B/s, 900 B left -> 90 s
    last = out.getvalue().split("\r")[-1]
    assert "≈ 1 min 30 s restantes" in last and "(estimado)" not in last


def test_eta_from_an_estimate_is_flagged_and_disappears_when_exceeded(monkeypatch):
    seen = []
    bar, out = _timed_progress(monkeypatch, [0.0, 10.0, 20.0], estimate=1000, on_progress=seen.append)
    bar(0)
    bar(100)
    assert "como mucho ≈ 1 min 30 s más (estimación)" in out.getvalue().split("\r")[-1]
    bar(1200)  # the export turned out larger than guessed: no more promises
    assert "como mucho" not in out.getvalue().split("\r")[-1]
    assert seen[-1] == 0.99  # the global bar never claims 100% before the transfer really ends


def test_no_eta_in_the_first_seconds(monkeypatch):
    bar, out = _timed_progress(monkeypatch, [0.0, 2.0])
    bar(0, 1000)
    bar(50, 1000)
    assert "restantes" not in out.getvalue()


def test_committed_space_is_spread_over_the_disks():
    from vm2morpheus.models import DiskInfo
    from vm2morpheus.sources.vmware import spread_committed
    disks = [DiskInfo("a", 30, "x"), DiskInfo("b", 10, "y")]
    spread_committed(disks, 8)
    assert [d.used_bytes for d in disks] == [6, 2]
    other = [DiskInfo("a", 30, "x")]
    spread_committed(other, 0)  # unknown: leave it alone
    assert other[0].used_bytes is None


def test_flat_url_from_the_disk_backing_file():
    from vm2morpheus.sources.vmware import flat_url
    url = flat_url("esxi1.lab", "[datastore 1] w2k16 sin virtio/w2k16_sin_virtio.vmdk", "ha-datacenter")
    assert url == ("https://esxi1.lab/folder/w2k16%20sin%20virtio/w2k16_sin_virtio-flat.vmdk"
                   "?dcPath=ha-datacenter&dsName=datastore%201")
    with pytest.raises(RuntimeError, match="No se entiende"):
        flat_url("h", "no-datastore.vmdk", "dc")


def test_measure_download_stops_after_the_requested_time_and_reports_bytes():
    from vm2morpheus.sources.vmware import measure_download
    t = {"now": 0.0}

    class Resp(_FakeResponse):
        def iter_content(self, size):
            for _ in range(1000):
                t["now"] += 1.0  # each chunk takes one "second"
                yield b"x" * 1024

    total, secs = measure_download(lambda *a, **k: Resp([]), "https://x", seconds=5, clock=lambda: t["now"])
    assert total == 5 * 1024 and secs == 5.0  # stopped as soon as 5 s had passed, not after 1000 chunks


def test_speedtest_endpoint_reports_both_routes(tmp_path):
    import urllib.request
    from vm2morpheus import gui

    class Fake:
        def connect(self): pass
        def close(self): pass

        def speed_test(self, name, seconds=20):
            return {"export": {"mib_s": 8.6, "mib": 172.0}, "flat": {"mib_s": 22.0, "mib": 440.0}}

    original = gui.make_source
    gui.make_source = lambda cfg: Fake()
    srv = HTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"source_type": "esxi", "source_host": "h", "source_user": "u", "source_password": "p",
                           "vm": "w2k16"}).encode()
        data = json.loads(urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{srv.server_port}/speedtest", data=body, method="POST")).read())
    finally:
        gui.make_source = original
        srv.shutdown()
    assert data["export"]["mib_s"] == 8.6 and data["flat"]["mib_s"] == 22.0


FOOTER_AND_EOS = b"x" * 512 + b"KDMV" + b"\x01" * 508 + b"\x00" * 512  # footer marker, footer header, end-of-stream


def test_download_stops_at_the_end_of_stream_marker_without_waiting_for_the_server(tmp_path):
    from vm2morpheus.sources.vmware import download_to

    class Hanging(_FakeResponse):
        def iter_content(self, size):
            yield b"grain-data" * 1000
            yield FOOTER_AND_EOS[:700]  # the end arrives split in two chunks
            yield FOOTER_AND_EOS[700:]
            raise AssertionError("kept reading after the end-of-stream marker (the real server never closes)")

    size = download_to("https://esxi/x", tmp_path / "d.vmdk", "c", False, lambda d, t: None,
                       getter=lambda *a, **k: Hanging([]))
    assert size == 10000 + len(FOOTER_AND_EOS)


def test_timeout_after_a_complete_stream_is_success_not_an_error(tmp_path):
    import requests
    from vm2morpheus.sources.vmware import download_to

    class Late(_FakeResponse):
        def iter_content(self, size):
            yield FOOTER_AND_EOS
            raise requests.exceptions.ReadTimeout("idle")

    assert download_to("https://esxi/x", tmp_path / "d.vmdk", "c", False, lambda d, t: None,
                       getter=lambda *a, **k: Late([])) == len(FOOTER_AND_EOS)


def test_ordinary_data_is_never_mistaken_for_the_end(tmp_path):
    from vm2morpheus.sources.vmware import vmdk_stream_complete
    assert vmdk_stream_complete(FOOTER_AND_EOS)
    assert not vmdk_stream_complete(b"\x00" * 2048)               # zeros but no footer header
    assert not vmdk_stream_complete(b"KDMV" + b"\x01" * 2044)      # header-like bytes but no end marker
    assert not vmdk_stream_complete(FOOTER_AND_EOS[:-1] + b"\x01")  # EOS marker not entirely zero
    assert not vmdk_stream_complete(b"short")


def test_complete_download_from_a_previous_run_is_reused(tmp_path):
    from vm2morpheus.sources.vmware import file_is_complete_vmdk
    done, partial = tmp_path / "a.vmdk", tmp_path / "b.vmdk"
    done.write_bytes(b"data" * 5000 + FOOTER_AND_EOS)
    partial.write_bytes(b"data" * 5000)
    assert file_is_complete_vmdk(done) and not file_is_complete_vmdk(partial)
    assert not file_is_complete_vmdk(tmp_path / "missing.vmdk")


def test_progress_observer_receives_label_bytes_and_unit(monkeypatch):
    import io
    from vm2morpheus import util
    seen = []
    monkeypatch.setattr(util.Progress, "observer", lambda label, done, total, unit: seen.append((label, done, total, unit)))
    util.Progress("descargar x", io.StringIO())(1024, None)
    util.Progress("convertir y", io.StringIO(), percent=True)(40, 100)
    assert seen == [("descargar x", 1024, None, "bytes"), ("convertir y", 40, 100, "percent")]


def test_job_endpoint_exposes_the_live_transfer(tmp_path):
    import urllib.request
    from vm2morpheus import gui
    gui.JOBS["tjob"] = {"status": "running", "cancel": threading.Event(),
                        "transfer": {"label": "descargar x", "done": 2048, "total": None, "unit": "bytes", "t": 1.5}}
    srv = HTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/job/tjob").read())
    finally:
        srv.shutdown()
        gui.JOBS.pop("tjob", None)
    assert data["transfer"]["done"] == 2048 and data["transfer"]["unit"] == "bytes"


def test_page_has_the_chart_and_the_log_toolbar():
    from vm2morpheus import gui
    page = gui.render_page()
    for needle in ('id="speedchart"', 'id="chartinfo"', "function feedChart", "function drawChart",
                   'onclick="copyLog()"', 'onclick="saveLog()"', 'onclick="clearLog()"', 'class="logbox"'):
        assert needle in page


def test_best_effort_does_not_wait_for_a_hung_call():
    import time as _t
    from vm2morpheus.sources.vmware import best_effort
    t0 = _t.monotonic()
    finished = best_effort(lambda: threading.Event().wait(), timeout=0.2)
    assert finished is False and _t.monotonic() - t0 < 2
    assert best_effort(lambda: None) is True and best_effort(lambda: 1 / 0) is True


def test_flat_download_uses_basic_auth_known_total_and_no_end_marker(tmp_path):
    from vm2morpheus.sources.vmware import download_to
    seen = {}

    def getter(url, **kw):
        seen.update(kw)
        return _FakeResponse([FOOTER_AND_EOS, b"more"])  # raw data may contain anything: never cut it short

    calls = []
    got = download_to("https://esxi/folder/x-flat.vmdk", tmp_path / "d.img", None, False,
                      lambda d, t: calls.append((d, t)), getter=getter, total=999, auth=("root", "pw"), end_marker=False)
    assert seen["auth"] == ("root", "pw") and seen["headers"] == {} and got == len(FOOTER_AND_EOS) + 4
    assert calls[-1] == (got, 999)


def _vmware_with_stubs(method, export, flat):
    from vm2morpheus.sources.vmware import VMwareSource
    src = VMwareSource({"host": "h", "username": "u", "password": "p", "download_method": method})
    calls = []
    src._fetch_disks = lambda vm, dest, progress: (calls.append("export"), export())[1]
    src._fetch_flat = lambda vm, dest, progress, parallel=False: (calls.append("flat-parallel" if parallel else "flat"), flat())[1]
    return src, calls


def test_auto_method_falls_back_to_the_flat_disk_when_the_export_stalls(tmp_path):
    def stalled():
        raise RuntimeError("El ESXi dejó de enviar datos durante 120 s (se habían descargado 5.30 GiB)")

    src, calls = _vmware_with_stubs("auto", stalled, lambda: ["flat-file"])
    assert src.fetch_disks(None, tmp_path, None) == ["flat-file"] and calls == ["export", "flat"]


def test_other_export_errors_and_export_only_do_not_fall_back(tmp_path):
    def broken():
        raise RuntimeError("Falló la exportación de la VM: licencia")

    src, calls = _vmware_with_stubs("auto", broken, lambda: ["x"])
    with pytest.raises(RuntimeError, match="licencia"):
        src.fetch_disks(None, tmp_path, None)
    assert calls == ["export"]  # a license problem would fail the flat route too: do not hide it

    def stalled():
        raise RuntimeError("El ESXi dejó de enviar datos durante 120 s")

    src2, calls2 = _vmware_with_stubs("export", stalled, lambda: ["x"])
    with pytest.raises(RuntimeError, match="dejó de enviar"):
        src2.fetch_disks(None, tmp_path, None)
    assert calls2 == ["export"]
    src3, calls3 = _vmware_with_stubs("flat", stalled, lambda: ["flat-only"])
    assert src3.fetch_disks(None, tmp_path, None) == ["flat-only"] and calls3 == ["flat"]


def test_stall_error_reports_the_bytes_and_the_last_data(tmp_path):
    import requests
    from vm2morpheus.sources.vmware import download_to

    class Stall(_FakeResponse):
        def iter_content(self, size):
            yield b"\x01\x02\x03\x04"
            raise requests.exceptions.ReadTimeout("idle")

    with pytest.raises(RuntimeError, match=r"4 bytes\)\. Últimos bytes recibidos: 01 02 03 04"):
        download_to("https://esxi/x", tmp_path / "d.vmdk", "c", False, lambda d, t: None, getter=lambda *a, **k: Stall([]))


def test_gui_passes_the_vmware_download_method():
    from vm2morpheus.gui import render_page, source_cfg
    base = {"source_type": "esxi", "source_host": "h", "source_user": "u", "source_password": "p"}
    assert source_cfg(dict(base))["download_method"] == "auto"
    assert source_cfg({**base, "vmware_method": "flat"})["download_method"] == "flat"
    assert 'id="vmware_method"' in render_page()


def test_raw_img_from_the_flat_download_converts_to_qcow2(tmp_path):
    from vm2morpheus.convert import convert_disk
    raw = tmp_path / "vm-disk0.img"
    subprocess.run(["qemu-img", "create", "-f", "raw", str(raw), "4M"], check=True, capture_output=True)
    raw.write_bytes(b"KDMV" + raw.read_bytes()[4:])  # even bytes that look like a VMDK header stay raw data
    out = convert_disk(raw, tmp_path / "vm-disk0.qcow2", "qcow2")
    assert out.read_bytes()[:4] == b"QFI\xfb"
    info = subprocess.run(["qemu-img", "info", "--output=json", str(out)], capture_output=True, text=True).stdout
    assert json.loads(info)["virtual-size"] == 4 * 1024 * 1024


def test_stall_watchdog_reports_where_the_frozen_thread_is_waiting():
    import io
    from vm2morpheus.util import StallWatchdog
    release = threading.Event()
    ready = threading.Event()

    def frozen_worker():
        ready.set()
        release.wait()  # stands in for a hung socket read / SOAP call / file write

    worker = threading.Thread(target=frozen_worker, daemon=True)
    worker.start()
    ready.wait()
    t = {"now": 0.0}
    out = io.StringIO()
    wd = StallWatchdog("la descarga de x.vmdk", after=45, repeat=60, stream=out, clock=lambda: t["now"])
    wd.tid = worker.ident
    wd.beat(100)
    t["now"] = 30.0
    assert wd.check() is False and out.getvalue() == ""       # still within the grace period
    t["now"] = 50.0
    assert wd.check() is True
    text = out.getvalue()
    assert "la descarga de x.vmdk lleva 50 s sin recibir datos" in text and "esperando en" in text
    assert "frozen_worker" in text or "release.wait" in text   # the stack names the blocked call site
    t["now"] = 80.0
    assert wd.check() is False                                 # not repeated before `repeat` seconds
    t["now"] = 115.0
    assert wd.check() is True
    wd.beat(200)                                               # data flows again: quiet
    t["now"] = 120.0
    assert wd.check() is False
    release.set()


def test_download_starts_and_stops_its_watchdog(tmp_path, monkeypatch):
    from vm2morpheus import util
    from vm2morpheus.sources import vmware
    events = []

    class Spy(util.StallWatchdog):
        def start(self):
            events.append("start")
            return super().start()

        def beat(self, value):
            events.append(("beat", value))
            super().beat(value)

        def stop(self):
            events.append("stop")
            super().stop()

    monkeypatch.setattr(vmware, "StallWatchdog", Spy)
    vmware.download_to("https://esxi/x", tmp_path / "d.vmdk", "c", False, lambda d, t: None,
                       getter=lambda *a, **k: _FakeResponse([b"aaaa", b"bb"]))
    assert events == ["start", ("beat", 4), ("beat", 6), "stop"]


def _migrate_with_reference(tmp_path, name, **flags):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    FakeMorpheus.instance_posts = 0
    for k, v in flags.items():
        setattr(FakeMorpheus, k, v)
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    msgs = []
    try:
        migrate(FakeSource(disk), _client(srv), "vm1", {"workdir": str(tmp_path / name), "shutdown_source": True},
                {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}}, log=msgs.append)
        return msgs, None
    except Exception as e:
        return msgs, e
    finally:
        srv.shutdown()
        for k in flags:
            setattr(FakeMorpheus, k, {"existing_name": None, "existing_image_name": None, "name_clash": False}[k])


def test_an_existing_instance_with_that_name_is_reported_before_anything_is_created(tmp_path):
    msgs, err = _migrate_with_reference(tmp_path, "w20", existing_name="vm1")
    assert isinstance(err, RuntimeError)
    assert "Ya existe en Morpheus una instancia llamada 'vm1' (id 99, estado running)" in str(err)
    assert "conserva lo ya descargado" in str(err) and FakeMorpheus.instance_posts == 0


def test_a_name_clash_the_precheck_missed_is_explained_and_not_retried_as_a_sata_problem(tmp_path):
    msgs, err = _migrate_with_reference(tmp_path, "w21", name_clash=True)
    assert isinstance(err, RuntimeError) and "Ya existe en Morpheus una instancia llamada 'vm1'" in str(err)
    assert FakeMorpheus.instance_posts == 1                       # one attempt: no retry without SATA
    assert not any("rechazó la petición del controlador SATA" in m for m in msgs)  # no misleading message


def test_volume_rejection_detection():
    from vm2morpheus.morpheus import MorpheusError
    from vm2morpheus.pipeline import volume_related_rejection
    assert volume_related_rejection(MorpheusError("POST /api/instances -> 400: invalid controllerMountPoint"))
    assert not volume_related_rejection(MorpheusError("POST /api/instances -> 400: Failed {'name': 'must be unique'}"))
    assert not volume_related_rejection(MorpheusError("POST /api/instances -> 500: controller exploded"))


def test_a_same_name_image_only_produces_a_warning(tmp_path):
    msgs, err = _migrate_with_reference(tmp_path, "w22", existing_image_name="vm1")
    assert err is None
    assert any("Aviso: ya hay una imagen llamada 'vm1' en Morpheus (id 5)" in m for m in msgs)


def test_progress_line_shows_average_and_current_speed(monkeypatch):
    import io
    from vm2morpheus import util
    clock = iter([0.0, 10.0, 20.0, 21.0, 25.0])
    monkeypatch.setattr(util.time, "monotonic", lambda: next(clock))
    out = io.StringIO()
    bar = util.Progress("descargar x", out)
    mib = 1024 * 1024
    bar(0)                   # t=0
    bar(100 * mib)           # t=10: 10 MiB/s
    bar(200 * mib)           # t=20: still 10 MiB/s
    bar(200 * mib + 1024)    # t=21: throttled (< 0.5 s since... no: 1 s later) -> printed
    bar(200 * mib + 2048)    # t=25: the last 4 s moved almost nothing
    last = out.getvalue().split("\r")[-1]
    assert "media 8.0 MiB/s" in last and "ahora 409.6 B/s" in last  # the last 5 s moved 2 KiB: the average hides it


def test_watchdog_warns_when_the_transfer_crawls_but_does_not_stop(tmp_path):
    import io
    from vm2morpheus.util import StallWatchdog
    done = threading.Event()
    ready = threading.Event()

    def worker():
        ready.set()
        done.wait()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    ready.wait()
    t = {"now": 0.0}
    out = io.StringIO()
    wd = StallWatchdog("la descarga de x", after=45, repeat=60, poll=5, stream=out, clock=lambda: t["now"],
                       min_rate=128 * 1024, slow_window=30)
    wd.tid = th.ident
    value = 0
    fired = []
    for sec in range(0, 70, 5):          # ~10 KiB/s: bytes keep arriving, so it is never "silent"
        t["now"] = float(sec)
        value += 50 * 1024
        wd.beat(value)
        fired.append(wd.check())
    text = out.getvalue()
    assert any(fired) and "va muy lenta" in text and "KiB/s en los últimos 30 s" in text
    assert "sin recibir datos" not in text
    done.set()


def test_watchdog_stays_quiet_at_a_healthy_rate():
    import io
    from vm2morpheus.util import StallWatchdog
    t = {"now": 0.0}
    out = io.StringIO()
    wd = StallWatchdog("x", stream=out, clock=lambda: t["now"], min_rate=128 * 1024, slow_window=30)
    wd.tid = threading.get_ident()
    value = 0
    for sec in range(0, 120, 5):
        t["now"] = float(sec)
        value += 40 * 1024 * 1024        # 8 MiB/s
        wd.beat(value)
        assert wd.check() is False
    assert out.getvalue() == ""


def test_tracker_books_time_per_step_even_when_steps_interleave():
    from vm2morpheus.tracker import Tracker
    t = {"now": 0.0}
    tr = Tracker(["fetch", "convert", "drivers"], [], clock=lambda: t["now"])
    tr.begin("fetch"); t["now"] = 100.0; tr.finish("fetch")
    tr.begin("convert"); t["now"] = 110.0                       # 10 s converting...
    tr.begin("drivers"); t["now"] = 140.0; tr.finish("drivers")  # ...30 s of driver work inside it...
    tr.begin("convert"); t["now"] = 145.0; tr.finish("convert")  # ...then 5 s more converting
    assert tr.durations == {"fetch": 100.0, "convert": 15.0, "drivers": 30.0}


def test_tracker_uses_learned_weights_and_falls_back_when_incomplete():
    from vm2morpheus.tracker import Tracker
    ev = []
    Tracker(["fetch", "convert", "upload"], ["fetch"], ev.append, weights={"fetch": 100, "convert": 10, "upload": 50})
    assert ev[0]["overall"] == pytest.approx(100 / 160)
    ev2 = []
    Tracker(["fetch", "convert", "upload"], ["fetch"], ev2.append, weights={"fetch": 100})  # a step is missing
    assert ev2[0]["overall"] == pytest.approx(0.30 / 0.85)  # default shares


def test_timings_scale_with_size_and_blend_runs(tmp_path):
    from vm2morpheus import timings
    assert timings.predict_weights(["fetch"], 10) is None       # nothing learned yet
    timings.record({"fetch": 600, "convert": 60, "upload": 300, "instance": 20}, gib=10)
    w = timings.predict_weights(["fetch", "convert", "upload", "instance"], gib=20)  # twice the data
    assert w == {"fetch": 1200.0, "convert": 120.0, "upload": 600.0, "instance": 20}  # fixed steps do not scale
    timings.record({"fetch": 1200}, gib=10)                     # a slow day: blended 50/50, not adopted outright
    assert timings.load()["fetch"] == pytest.approx((60 + 120) / 2)
    assert timings.predict_weights(["fetch", "drivers"], 5) is None  # drivers was never timed


def test_step_detail_makes_the_bar_agree_with_the_log_line():
    from vm2morpheus.tracker import Tracker
    ev = []
    tr = Tracker(["fetch", "convert", "drivers", "upload"], [], ev.append)
    tr.begin("drivers")
    tr.update("drivers", 0.4 * 0.78, "convirtiendo el disco a VHDX", 0.78)
    assert ev[-1]["label"] == "inyección de drivers · convirtiendo el disco a VHDX"
    assert ev[-1]["fraction"] == pytest.approx(0.78)            # what the log line says
    assert ev[-1]["overall"] == pytest.approx(0.1 / 0.95 * 0.4 * 0.78)  # while the whole job barely moved
    tr.update("drivers", 0.6, "instalando los drivers con DISM", hide_percent=True)
    assert ev[-1]["fraction"] is None                            # DISM reports no percentage
    tr.finish("drivers")
    assert ev[-1]["label"] == "" and ev[-1]["step"] is None


def test_pipeline_vhdx_conversion_feeds_the_drivers_step_and_a_run_records_timings(tmp_path, monkeypatch):
    from vm2morpheus import pipeline, timings
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    monkeypatch.setattr(pipeline, "_is_windows", lambda: True)
    monkeypatch.setattr(pipeline, "is_admin", lambda: True)
    monkeypatch.setattr(pipeline, "ensure_virtio_iso", lambda p, progress=None: "x.iso")
    monkeypatch.setattr(pipeline, "inject_virtio_windows", lambda path, iso, log: ["viostor"])

    class Win(FakeSource):
        def get_vm(self, name):
            vm = super().get_vm(name)
            vm.os_family = "windows"
            return vm

    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    events = []
    pipeline.migrate(Win(disk), _client(srv), "vm1", {"workdir": str(tmp_path / "w30"), "shutdown_source": True,
                     "inject_drivers": True}, {"image_type": "qcow2"}, log=lambda *_: None, on_progress=events.append)
    srv.shutdown()
    vhdx = [e for e in events if e["step"] == "drivers" and "VHDX" in e["label"]]
    assert vhdx and vhdx[-1]["fraction"] == pytest.approx(1.0)   # the VMDK -> VHDX step now moves the bar
    assert not any(e["step"] == "convert" and e["fraction"] == 0 and e["overall"] > 0.2 and "VHDX" in e["label"]
                   for e in events)
    learned = timings.load()
    assert {"fetch", "convert", "drivers", "upload"} <= set(learned)   # this run was timed for the next one


def test_network_is_mandatory_when_the_vm_is_created_from_scratch_and_checked_before_any_work():
    from vm2morpheus.gui import make_cfgs
    base = {"vm": "w2k16", "morpheus_url": "https://m", "morpheus_token": "t", "source_type": "esxi",
            "source_host": "h", "source_user": "u", "source_password": "p",
            "cloud_id": "1", "group_id": "1", "plan_id": "23"}
    with pytest.raises(ValueError, match="Elige la red de la VM nueva"):
        make_cfgs(dict(base))                                   # from scratch, no network: refuse up front
    _, m, _ = make_cfgs({**base, "network_id": "1"})
    assert m["reference_instance"]["synthetic"]["interfaces"] == [{"id": "network-1", "network": {"id": 1}}]
    _, m2, _ = make_cfgs({**base, "ref_instance_id": "5"})      # a reference instance brings its own network
    assert m2["reference_instance"]["instance_id"] == 5
    _, m3, _ = make_cfgs({k: v for k, v in base.items() if k not in ("cloud_id", "group_id", "plan_id")})
    assert "reference_instance" not in m3                       # image only: nothing to validate


def test_the_new_name_is_used_for_the_image_and_the_vm_but_not_for_the_work_folder(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    msgs = []
    migrate(FakeSource(disk), _client(srv), "vm1", {"workdir": str(tmp_path / "w31"), "shutdown_source": True,
            "new_name": "  prod-w2k16  "}, {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}},
            log=msgs.append)
    srv.shutdown()
    sent = dict(FakeMorpheus.log)
    assert sent["image"]["virtualImage"]["name"] == "prod-w2k16"
    assert sent["instance"]["instance"]["name"] == "prod-w2k16"
    assert (tmp_path / "w31" / "vm1" / "state.json").exists()      # resume state stays keyed by the source name
    assert any("Nombre en Morpheus: 'prod-w2k16' (en el origen es 'vm1')" in m for m in msgs)


def test_without_a_new_name_the_original_is_kept(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    migrate(FakeSource(disk), _client(srv), "vm1", {"workdir": str(tmp_path / "w32"), "shutdown_source": True,
            "new_name": None}, {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}},
            log=lambda *_: None)
    srv.shutdown()
    assert dict(FakeMorpheus.log)["instance"]["instance"]["name"] == "vm1"


def test_page_follows_the_source_name_until_the_user_types_their_own():
    from vm2morpheus.gui import render_page
    page = render_page()
    for needle in ('id="new_vm_name"', "function syncNewName", "newNameTouched=true", "(elige una red)",
                   "Red <small>(obligatoria)</small>"):
        assert needle in page


def test_plan_shortfall_message():
    from vm2morpheus.pipeline import plan_shortfall
    small = {"name": "Discovered Instance", "maxCores": 1, "maxMemory": 512 * 2**20}
    text = plan_shortfall(small, cpu=2, memory_mb=8192)
    assert "Aviso: el plan 'Discovered Instance' da 1 CPU y 512.0 MiB" in text and "2 vCPU y 8192 MiB" in text
    assert plan_shortfall({"name": "ok", "maxCores": 2, "maxMemory": 8192 * 2**20}, 2, 8192) is None
    assert plan_shortfall({"name": "bigger", "maxCores": 4, "maxMemory": 16384 * 2**20}, 2, 8192) is None
    assert plan_shortfall({"name": "unknown size"}, 2, 8192) is None   # nothing to compare: stay quiet
    assert plan_shortfall(None, 2, 8192) is None


def test_the_run_warns_when_the_chosen_plan_is_smaller_than_the_vm(tmp_path):
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    FakeMorpheus.log.clear()
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    msgs = []
    migrate(FakeSource(disk), _client(srv), "vm1", {"workdir": str(tmp_path / "w33"), "shutdown_source": True},
            {"image_type": "qcow2", "reference_instance": {"instance_id": 5, "image_id": 3}}, log=msgs.append)
    srv.shutdown()
    # FakeSource is a 2 vCPU / 2048 MiB VM; fake plan 6 gives 1 CPU / 512 MiB
    assert any(m.startswith("Aviso: el plan '1 CPU, 512MB Memory' da 1 CPU y 512.0 MiB") for m in msgs)
    assert "instance" in dict(FakeMorpheus.log)   # a warning, never a block


def test_page_picks_the_plan_that_matches_the_vm():
    from vm2morpheus.gui import render_page
    page = render_page()
    for needle in ("function pickPlan", 'id="planhint"', "data-cores", "planTouched=true", "coincide con la VM original"):
        assert needle in page


def test_timings_are_kept_separately_per_kind_of_source(tmp_path):
    from vm2morpheus import timings
    timings.record({"fetch": 100, "convert": 20, "upload": 50}, gib=10, kind="hyperv")
    timings.record({"fetch": 1380, "convert": 220, "upload": 390}, gib=5.3, kind="vmware")
    hv = timings.predict_weights(["fetch", "convert", "upload"], 10, "hyperv")
    vm = timings.predict_weights(["fetch", "convert", "upload"], 10, "vmware")
    assert hv["fetch"] == pytest.approx(100) and vm["fetch"] == pytest.approx(1380 / 5.3 * 10)
    assert vm["fetch"] > 20 * hv["fetch"]                      # an ESXi export is not an SMB copy
    assert timings.predict_weights(["fetch"], 10, "other") is None


def test_an_old_flat_timings_file_is_ignored_not_misread(tmp_path):
    from vm2morpheus import timings
    timings.PATH.parent.mkdir(parents=True, exist_ok=True)
    timings.PATH.write_text(json.dumps({"fetch": 60.0, "convert": 5.0, "upload": 30.0}))   # pre-0.19.3 layout
    assert timings.predict_weights(["fetch", "convert", "upload"], 1, "hyperv") is None
    timings.record({"fetch": 10}, 1, "hyperv")                  # and writing afterwards works
    assert timings.load("hyperv")["fetch"] == pytest.approx(10)


def test_each_source_declares_its_timing_kind():
    from vm2morpheus.sources.hyperv import HyperVSource
    from vm2morpheus.sources.vmware import VMwareSource
    assert HyperVSource.timing_kind == "hyperv" and VMwareSource.timing_kind == "vmware"
    assert HyperVSource.__doc__ and VMwareSource.__doc__        # the attribute must not displace the docstring


def test_compressed_qcow2_is_smaller_valid_and_opt_in(tmp_path):
    from vm2morpheus.convert import convert_disk
    raw = tmp_path / "d.img"
    raw.write_bytes((b"windows-like compressible data " * 4096) * 64)   # ~8 MiB of repeating data
    plain = convert_disk(raw, tmp_path / "plain.qcow2", "qcow2")
    small = convert_disk(raw, tmp_path / "small.qcow2", "qcow2", compress=True)
    assert small.stat().st_size < plain.stat().st_size * 0.5
    assert subprocess.run(["qemu-img", "check", str(small)], capture_output=True).returncode == 0
    # same virtual contents either way
    back = tmp_path / "back.img"
    subprocess.run(["qemu-img", "convert", "-O", "raw", str(small), str(back)], check=True, capture_output=True)
    assert back.read_bytes() == raw.read_bytes()


def test_pipeline_compresses_only_when_asked(tmp_path, monkeypatch):
    from vm2morpheus import pipeline
    seen = []
    real = pipeline.convert_disk
    monkeypatch.setattr(pipeline, "convert_disk",
                        lambda src, dst, fmt, bar, cancel, compress=False: (seen.append(compress),
                                                                           real(src, dst, fmt, bar, cancel, compress=compress))[1])
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    for name, opts_extra in (("w40", {}), ("w41", {"compress_image": True})):
        srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        pipeline.migrate(FakeSource(disk), _client(srv), "vm1",
                         {"workdir": str(tmp_path / name), "shutdown_source": True, **opts_extra},
                         {"image_type": "qcow2"}, log=lambda *_: None)
        srv.shutdown()
    assert seen == [False, True]


def _ranged_response(status, chunks):
    class R(_FakeResponse):
        status_code = status
    return R(chunks)


def test_parallel_measurement_adds_up_the_streams_and_spreads_the_offsets():
    from vm2morpheus.sources.vmware import measure_parallel
    ranges = []

    def getter(url, **kw):
        ranges.append(kw["headers"]["Range"])
        return _ranged_response(206, [b"x" * 1000, b"y" * 1000])

    total, secs, ranged = measure_parallel(getter, "https://esxi/x", size=4000, streams=4, seconds=60)
    assert ranged is True and total == 4 * 2000
    assert sorted(ranges) == ["bytes=0-", "bytes=1000-", "bytes=2000-", "bytes=3000-"]


def test_parallel_measurement_detects_a_server_without_range_support():
    from vm2morpheus.sources.vmware import measure_parallel
    total, secs, ranged = measure_parallel(lambda url, **kw: _ranged_response(200, [b"x" * 10]),
                                           "https://esxi/x", size=4000, streams=4, seconds=60)
    assert ranged is False and total == 0


def test_page_reports_parallel_speed_and_offers_compression():
    from vm2morpheus.gui import render_page
    page = render_page()
    for needle in ("conexiones a la vez", "flat_parallel", 'id="compress_image"'):
        assert needle in page


def _ico_sizes(data: bytes) -> list[int]:
    reserved, kind, count = int.from_bytes(data[0:2], "little"), int.from_bytes(data[2:4], "little"), int.from_bytes(data[4:6], "little")
    assert reserved == 0 and kind == 1                       # a real .ico header
    return sorted((data[6 + 16 * i] or 256) for i in range(count))


def test_exe_icon_has_every_size_windows_asks_for():
    from pathlib import Path as P
    ico = (P(__file__).resolve().parent.parent / "assets" / "vm2morpheus.ico").read_bytes()
    assert _ico_sizes(ico) == [16, 24, 32, 48, 64, 128, 256]


def test_favicon_and_png_icon_are_served_and_valid():
    import urllib.request
    from vm2morpheus import gui
    srv = HTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        fav = urllib.request.urlopen(base + "/favicon.ico")
        png = urllib.request.urlopen(base + "/icon.png")
        fav_body, png_body = fav.read(), png.read()
        assert fav.headers["Content-Type"] == "image/x-icon" and png.headers["Content-Type"] == "image/png"
    finally:
        srv.shutdown()
    assert _ico_sizes(fav_body) == [16, 32, 48]
    assert png_body[:8] == b"\x89PNG\r\n\x1a\n"


def test_page_uses_the_globe_as_favicon_and_the_build_embeds_it_in_the_exe():
    from vm2morpheus import gui
    page = gui.render_page()
    assert '<link rel="icon" href="/favicon.ico" sizes="any">' in page and 'rel="apple-touch-icon"' in page
    assert '<link rel="icon" href="/logo.png">' not in page   # the wide logo is no longer used as the tab icon
    wf = open(".github/workflows/build-exe.yml", encoding="utf-8").read()
    assert "--icon assets/vm2morpheus.ico" in wf


# ---------------------------------------------------------------- history
def _clock(values):
    it = iter(values)
    return lambda: next(it)


def test_recorder_turns_samples_into_speeds_bytes_and_a_curve():
    from vm2morpheus import history
    MiB = 1024 * 1024
    # started at t=1000; a download at 10 MiB/s for 2 samples, then an upload at 40 MiB/s
    rec = history.Recorder({"vm": "w2k16", "gib": 1.0}, clock=_clock([1000.0, 1002.0, 1004.0, 1010.0, 1011.0, 1012.0, 1020.0]))
    rec.sample("descargar d.vmdk", 0, None, "bytes")        # t=1002
    rec.sample("descargar d.vmdk", 20 * MiB, None, "bytes")  # t=1004
    rec.sample("convertir d", 50, 100, "percent")            # t=1010 (not a byte transfer: ignored)
    rec.sample("subir d.qcow2", 0, 100 * MiB, "bytes")       # t=1011
    rec.sample("subir d.qcow2", 40 * MiB, 100 * MiB, "bytes")  # t=1012
    entry = rec.finish("ok", {"fetch": 4.0, "upload": 2.0, "convert": 3.0}, result={"instance": {"id": 7}, "images": [{"id": 1}]})
    assert entry["status"] == "ok" and entry["vm"] == "w2k16" and entry["total_seconds"] == 20.0
    assert entry["bytes"] == {"fetch": 20 * MiB, "upload": 40 * MiB}
    assert entry["speeds_mib_s"] == {"fetch": 5.0, "upload": 20.0}          # bytes / time spent in that step
    assert [p["up"] for p in entry["series"]] == [False, True]
    assert entry["series"][0]["y"] == pytest.approx(10.0) and entry["series"][1]["y"] == pytest.approx(40.0)
    assert entry["instance"] == {"id": 7} and history.get(entry["id"])["vm"] == "w2k16"   # persisted


def test_failed_runs_are_recorded_with_their_error():
    from vm2morpheus import history
    rec = history.Recorder({"vm": "x"}, clock=_clock([0.0, 5.0]))
    e = rec.finish("error", {"fetch": 3.0}, error="Falló PowerShell: algo")
    assert e["status"] == "error" and e["error"] == "Falló PowerShell: algo" and e["speeds_mib_s"] == {}
    assert history.load()[-1]["id"] == e["id"]


def test_history_is_capped_deletable_and_survives_a_corrupt_file():
    from vm2morpheus import history
    for i in range(history.MAX_ENTRIES + 5):
        history.add({"id": f"e{i}", "status": "ok"})
    entries = history.load()
    assert len(entries) == history.MAX_ENTRIES and entries[0]["id"] == "e5"       # oldest dropped first
    assert history.delete("e10") is True and history.delete("nope") is False
    assert history.update("e11", image_deleted=True) and history.get("e11")["image_deleted"] is True
    history.clear()
    assert history.load() == []
    history.PATH.write_text("{not json")
    assert history.load() == []                                                   # corrupt file: start empty
    history.add({"id": "again"})
    assert [e["id"] for e in history.load()] == ["again"]


def test_downsample_keeps_the_phase_and_the_overall_shape():
    from vm2morpheus.history import downsample
    pts = [{"x": float(i), "y": 10.0 if i < 500 else 40.0, "up": i >= 500} for i in range(1000)]
    out = downsample(pts, 100)
    assert len(out) <= 100 and out[0]["up"] is False and out[-1]["up"] is True
    assert out[0]["y"] == pytest.approx(10.0) and out[-1]["y"] == pytest.approx(40.0)
    assert downsample(pts[:50], 100) == pts[:50]                                   # short curves untouched


def test_summary_and_csv():
    from vm2morpheus import history
    GiB = 1024 ** 3
    entries = [
        {"id": "a", "status": "ok", "started": "2026-10-31T10:00:00", "vm": "w2k16", "target_name": "w2k16", "source_type": "esxi",
         "source_host": "esxi1", "gib": 5.3, "total_seconds": 2400, "bytes": {"upload": 10 * GiB},
         "speeds_mib_s": {"fetch": 4.0, "upload": 26.0}, "instance": {"id": 37}, "image_deleted": True},
        {"id": "b", "status": "error", "vm": "w2k19", "error": "boom; con punto y coma", "total_seconds": 60},
    ]
    s = history.summarize(entries)
    assert s["runs"] == 2 and s["ok"] == 1 and s["gib_moved"] == pytest.approx(10.0)
    assert s["avg_download"] == 4.0 and s["avg_upload"] == 26.0 and s["avg_seconds"] == 2400
    rows = history.to_csv(entries).strip().splitlines()
    assert rows[0].startswith("fecha;estado;vm_origen") and "w2k16;w2k16;esxi:esxi1;5.30;2400;4.0;26.0;37;sí" in rows[1]
    assert '"boom; con punto y coma"' in rows[2]                                  # quoted, so the CSV stays valid


def test_migrate_exposes_the_tracker_even_when_the_run_fails(tmp_path):
    from vm2morpheus import pipeline
    info = {}
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    with pytest.raises(Exception):
        pipeline.migrate(FakeSource(disk), MorpheusClient("http://127.0.0.1:1", "t"), "vm1",
                         {"workdir": str(tmp_path / "w50"), "shutdown_source": True}, {"image_type": "qcow2"},
                         log=lambda *_: None, run_info=info)                      # no Morpheus listening: the upload fails
    assert info["vm"].name == "vm1" and info["target"] == "vm1" and info["gib"] > 0
    assert set(info["tracker"].snapshot()) >= {"fetch", "convert"}                # the finished steps were timed


# ------------------------------------------------- GUI: history recording and the clean-up after a migration
def _form(tmp_path, port, **extra):
    return {"vm": "vm1", "morpheus_url": f"http://127.0.0.1:{port}", "morpheus_token": "tok", "source_type": "esxi",
            "source_host": "esxi1", "source_user": "root", "source_password": "pw", "shutdown": True,
            "ref_instance_id": "5", "workdir": str(tmp_path / "work"), **extra}


def _run_job(tmp_path, monkeypatch, **extra):
    """Run gui.run_job against the fake source and the fake Morpheus; returns (job_id, job)."""
    from vm2morpheus import gui
    disk = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(disk), "8M"], check=True, capture_output=True)
    monkeypatch.setattr(gui, "make_source", lambda cfg: FakeSource(disk))
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    jid = uuid_hex()
    gui.JOBS[jid] = {"status": "running", "cancel": threading.Event()}
    cancel_first = extra.pop("_cancel", False)
    if cancel_first:
        gui.JOBS[jid]["cancel"].set()
    try:
        gui.run_job(jid, _form(tmp_path, srv.server_port, **extra), False)
    finally:
        srv.shutdown()
    return jid, gui.JOBS[jid]


def uuid_hex():
    import uuid
    return uuid.uuid4().hex[:8]


def test_a_successful_gui_run_is_written_to_the_history(tmp_path, monkeypatch):
    from vm2morpheus import gui, history
    FakeMorpheus.log.clear()
    jid, job = _run_job(tmp_path, monkeypatch)
    assert job["status"] == "done"
    entry = history.get(job["history_id"])
    assert entry["status"] == "ok" and entry["vm"] == "vm1" and entry["source_host"] == "esxi1"
    assert entry["instance"]["id"] == 42 and entry["images"] and entry["bytes"]["upload"] > 0
    assert {"fetch", "convert", "upload"} <= set(entry["steps"]) and entry["vcpu"] == 2 and entry["disks"]
    info = gui.done_info(job)
    assert info["instance"]["id"] == 42 and info["history_id"] == entry["id"] and info["vm"] == "vm1"


def test_failed_and_cancelled_gui_runs_are_recorded_too(tmp_path, monkeypatch):
    from vm2morpheus import history
    jid, job = _run_job(tmp_path, monkeypatch, vm="   ")        # a missing VM name fails before anything starts
    assert job["status"] == "error"
    failed = history.get(job["history_id"])
    assert failed["status"] == "error" and "nombre de la VM" in failed["error"]
    jid2, job2 = _run_job(tmp_path, monkeypatch, _cancel=True)
    cancelled = history.get(job2["history_id"])
    assert job2["status"] == "cancelled" and cancelled["status"] == "cancelled"


def test_done_info_is_only_offered_when_a_vm_was_really_created():
    from vm2morpheus import gui
    base = {"morpheus": {"url": "https://m/"}, "vm": "vm1", "workdir": "x"}
    ok = {"status": "done", "cleanup": {**base, "result": {"instance": {"id": 1}, "images": [{"id": 2, "name": "n"}]}}}
    assert gui.done_info(ok)["morpheus_url"] == "https://m"
    assert gui.done_info({**ok, "status": "error"}) is None
    assert gui.done_info({"status": "done", "cleanup": {**base, "result": {"dry_run": True}}}) is None
    assert gui.done_info({"status": "done", "cleanup": {**base, "result": {"instance": None, "images": [{"id": 2}]}}}) is None
    assert gui.done_info({"status": "done"}) is None


def test_cleanup_deletes_the_image_and_the_work_folder_and_updates_the_history(tmp_path, monkeypatch):
    from vm2morpheus import gui, history
    FakeMorpheus.log.clear()
    FakeMorpheus.deleted.clear()
    FakeMorpheus.refuse_delete = False
    jid, job = _run_job(tmp_path, monkeypatch)
    port = job["cleanup"]["morpheus"]["url"].rsplit(":", 1)[1]
    srv = HTTPServer(("127.0.0.1", int(port)), FakeMorpheus)  # same URL again: the run's server was shut down
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = gui.cleanup({"job": jid, "delete_workdir": True})
    finally:
        srv.shutdown()
    assert out["image_deleted"] is True and out["workdir_removed"] is True and out["images"][0]["ok"]
    assert FakeMorpheus.deleted == ["/api/virtual-images/7"]
    assert not (tmp_path / "work" / "vm1").exists()
    entry = history.get(job["history_id"])
    assert entry["image_deleted"] is True and entry["workdir_removed"] is True


def test_cleanup_reports_a_refusal_from_morpheus_and_keeps_the_work_folder(tmp_path, monkeypatch):
    from vm2morpheus import gui, history
    FakeMorpheus.deleted.clear()
    jid, job = _run_job(tmp_path, monkeypatch)
    port = int(job["cleanup"]["morpheus"]["url"].rsplit(":", 1)[1])
    FakeMorpheus.refuse_delete = True
    srv = HTTPServer(("127.0.0.1", port), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = gui.cleanup({"job": jid, "delete_workdir": True})
    finally:
        srv.shutdown()
        FakeMorpheus.refuse_delete = False
    assert out["image_deleted"] is False and out["workdir_removed"] is False
    assert "Image is in use by instance 37" in out["images"][0]["error"]        # Morpheus' own words, not hidden
    assert (tmp_path / "work" / "vm1" / "state.json").exists()
    assert history.get(job["history_id"])["image_deleted"] is False


def test_cleanup_refuses_when_there_is_nothing_to_clean():
    from vm2morpheus import gui
    with pytest.raises(ValueError, match="nada que limpiar"):
        gui.cleanup({"job": "does-not-exist"})


def test_safe_remove_workdir_only_touches_the_one_state_folder(tmp_path):
    from vm2morpheus.gui import safe_remove_workdir
    work = tmp_path / "work"
    (work / "vm1").mkdir(parents=True)
    (work / "vm1" / "state.json").write_text("{}")
    (work / "keepme").mkdir()
    (work / "keepme" / "important.vhdx").write_text("source disk")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "state.json").write_text("{}")
    assert safe_remove_workdir(str(work), "keepme") is False          # no state.json: not ours
    assert safe_remove_workdir(str(work), "../outside") is False      # not a direct child of the work folder
    assert safe_remove_workdir(str(work), "missing") is False
    assert (work / "keepme" / "important.vhdx").exists() and (tmp_path / "outside" / "state.json").exists()
    assert safe_remove_workdir(str(work), "vm1") is True and not (work / "vm1").exists()


def test_history_endpoints_over_http(tmp_path):
    import urllib.request
    from vm2morpheus import gui, history
    history.add({"id": "aa11", "status": "ok", "vm": "w2k16", "started": "2026-10-31T10:00:00", "total_seconds": 60,
                 "gib": 5.3, "speeds_mib_s": {"fetch": 4.0, "upload": 26.0}, "bytes": {"upload": 10 * 1024 ** 3},
                 "series": [{"x": 1.0, "y": 5.0, "up": False}]})
    history.add({"id": "bb22", "status": "error", "vm": "w2k19", "error": "boom"})
    srv = HTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        listing = json.loads(urllib.request.urlopen(base + "/history").read())
        assert [e["id"] for e in listing["entries"]] == ["bb22", "aa11"]          # newest first
        assert all("series" not in e for e in listing["entries"]) and listing["summary"]["runs"] == 2
        assert json.loads(urllib.request.urlopen(base + "/history/aa11").read())["series"][0]["y"] == 5.0
        csv_resp = urllib.request.urlopen(base + "/history.csv")
        assert csv_resp.headers["Content-Type"].startswith("text/csv") and csv_resp.read().startswith(b"\xef\xbb\xbffecha;")
        post = lambda path, body: json.loads(urllib.request.urlopen(urllib.request.Request(  # noqa: E731
            base + path, data=json.dumps(body).encode(), method="POST")).read())
        assert post("/history/delete", {"id": "bb22"}) == {"ok": True} and post("/history/delete", {"id": "zz"}) == {"ok": False}
        assert [e["id"] for e in history.load()] == ["aa11"]
        assert post("/history/clear", {}) == {"ok": True} and history.load() == []
    finally:
        srv.shutdown()


def test_page_has_the_history_tab_and_the_two_step_delete_dialog():
    from vm2morpheus import gui
    page = gui.render_page()
    for needle in ('id="tab-historico"', 'id="tabbtn-historico"', "function loadHistory", "function showDetail",
                   "function showDoneDialog", "Pulsa otra vez para confirmar el borrado", "Reintentar el borrado",
                   "El borrado no se puede deshacer", 'href="/history.csv"', "function drawSeriesChart"):
        assert needle in page


# ------------------------------------------------------------ hot copy (Hyper-V checkpoint, source stays on)
def _hv(**cfg):
    from vm2morpheus.sources.hyperv import HyperVSource
    return HyperVSource({"host": "h", "username": "u", "password": "p", **cfg})


def test_hyperv_hot_prepare_checkpoints_the_vm_and_returns_the_frozen_base_disks():
    src = _hv()
    scripts = []

    def ps(script):
        scripts.append(script)
        return json.dumps({"Type": "Production", "Disks": [
            {"Path": r"D:\VMs\w2k16\w2k16.vhdx", "Size": 85899345920, "FileSize": 9663676416},
            {"Path": r"E:\Datos\datos.vhdx", "Size": 107374182400, "FileSize": 45056}]})

    src._ps = ps
    vm = VMInfo("w2k16", 2, 8192, "bios", "windows", "on", [DiskInfo("w2k16-checkpoint.avhdx", 1, "x", index=0)])
    disks = src.hot_prepare(vm)
    script = scripts[0]
    assert "Checkpoint-VM" in script and "Get-VMHardDiskDrive -VMSnapshot" in script and "'w2k16'" in script
    assert "-eq 'Disabled'" in script                              # refuses VMs with checkpoints turned off
    assert [d.path for d in disks] == [r"D:\VMs\w2k16\w2k16.vhdx", r"E:\Datos\datos.vhdx"]
    assert [d.name for d in disks] == ["w2k16.vhdx", "datos.vhdx"] and [d.index for d in disks] == [0, 1]
    assert disks[0].used_bytes == 9663676416 and "VSS" in src.hot_type
    assert src._hot[0] == "w2k16" and src._hot[1].startswith("vm2morpheus-")


def test_hyperv_hot_release_removes_only_our_checkpoint_and_is_safe_to_call_twice():
    src = _hv()
    src._ps = lambda script: json.dumps({"Type": "Standard", "Disks": []})
    vm = VMInfo("w2k16", 1, 1024, "bios", "windows", "on", [])
    src.hot_prepare(vm)
    name = src._hot[1]
    scripts, logs = [], []
    src._ps = lambda script: scripts.append(script) or ""
    src.hot_release(logs.append)
    assert "Remove-VMSnapshot" in scripts[0] and f"'{name}'" in scripts[0] and "'w2k16'" in scripts[0]
    assert src._hot is None and any("Checkpoint temporal eliminado" in m for m in logs)
    src.hot_release(logs.append)                                    # nothing left to do
    assert len(scripts) == 1


def test_hyperv_hot_release_failure_keeps_the_checkpoint_known_and_raises():
    src = _hv()
    src._ps = lambda script: json.dumps({"Type": "Standard", "Disks": []})
    src.hot_prepare(VMInfo("w2k16", 1, 1024, "bios", "windows", "on", []))

    def boom(script):
        raise RuntimeError("Falló PowerShell en h: acceso denegado")

    src._ps = boom
    with pytest.raises(RuntimeError, match="acceso denegado"):
        src.hot_release()
    assert src._hot is not None                                     # so a later attempt can still clean it


def test_a_half_done_checkpoint_attempt_is_still_remembered_for_cleanup():
    src = _hv()

    def fails_after_creating(script):
        raise RuntimeError("Falló PowerShell en h: Get-VHD no pudo leer el disco")

    src._ps = fails_after_creating
    with pytest.raises(RuntimeError):
        src.hot_prepare(VMInfo("w2k16", 1, 1024, "bios", "windows", "on", []))
    assert src._hot and src._hot[0] == "w2k16"                      # hot_release will remove it if it exists


def test_hyperv_hot_copy_explains_disabled_checkpoints():
    src = _hv()

    def disabled(script):
        raise RuntimeError("Falló PowerShell en h: CHECKPOINTS_DISABLED")

    src._ps = disabled
    with pytest.raises(RuntimeError, match=r"Set-VM -Name 'w2k16' -CheckpointType Production"):
        src.hot_prepare(VMInfo("w2k16", 1, 1024, "bios", "windows", "on", []))


def test_hyperv_get_vm_reports_the_names_of_existing_checkpoints():
    src = _hv()
    src._ps = lambda script: json.dumps({"Name": "w2k16", "Cpu": 1, "MemoryMB": 1024, "Generation": 1, "State": "Running",
                                         "Disks": [], "Nets": [], "Snapshots": 2,
                                         "SnapshotNames": ["antes de actualizar", "vm2morpheus-20261031-1010"]})
    vm = src.get_vm("w2k16")
    assert vm.has_snapshots and vm.snapshot_names == ["antes de actualizar", "vm2morpheus-20261031-1010"]


class HotSource(FakeSource):
    supports_hot = True
    hot_type = "estándar, consistente ante fallos"

    def __init__(self, disk, running=True, fetch_error=None, release_error=None, snapshots=None):
        super().__init__(disk)
        self.running, self.fetch_error, self.release_error, self.snapshots = running, fetch_error, release_error, snapshots
        self.calls = []

    def get_vm(self, name):
        vm = VMInfo(name, 2, 2048, "bios", "windows", "on" if self.running else "off",
                    [DiskInfo("w2k16.vhdx", 8 << 20, "x", index=0)])
        vm.has_snapshots, vm.snapshot_names = bool(self.snapshots), list(self.snapshots or [])
        return vm

    def power_off(self, name):
        self.calls.append("power_off")

    def hot_prepare(self, vm):
        self.calls.append("hot_prepare")
        return [DiskInfo("frozen-parent.vhdx", d.size_bytes, "frozen", index=d.index) for d in vm.disks]

    def fetch_disks(self, vm, dest, progress):
        self.calls.append("fetch:" + ",".join(d.name for d in vm.disks))
        if self.fetch_error:
            raise self.fetch_error
        out = dest / "d.vmdk"
        shutil.copy(self.disk, out)
        return [out]

    def hot_release(self, log=print):
        self.calls.append("hot_release")
        if self.release_error:
            raise self.release_error


def _hot_run(tmp_path, source, name, opts_extra=None, dry_run=False):
    srv = HTTPServer(("127.0.0.1", 0), FakeMorpheus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    msgs = []
    try:
        res = migrate(source, _client(srv), "vm1", {"workdir": str(tmp_path / name), "hot_copy": True, **(opts_extra or {})},
                      {"image_type": "qcow2"}, log=msgs.append, dry_run=dry_run)
        return res, msgs, None
    except BaseException as e:  # noqa: BLE001 - the tests inspect CancelledError too
        return None, msgs, e
    finally:
        srv.shutdown()


def _disk(tmp_path):
    d = tmp_path / "src.vmdk"
    subprocess.run(["qemu-img", "create", "-f", "vmdk", str(d), "8M"], check=True, capture_output=True)
    return d


def test_hot_copy_keeps_the_vm_on_copies_the_frozen_disks_and_releases_the_checkpoint(tmp_path):
    src = HotSource(_disk(tmp_path))
    res, msgs, err = _hot_run(tmp_path, src, "h1")
    assert err is None and res["hot"] is True
    assert src.calls == ["hot_prepare", "fetch:frozen-parent.vhdx", "hot_release"]   # never powered off
    assert any("Checkpoint estándar, consistente ante fallos" in m and "sigue encendida" in m for m in msgs)


def test_hot_copy_releases_the_checkpoint_when_the_copy_fails_or_is_cancelled(tmp_path):
    from vm2morpheus.util import CancelledError
    for name, error in (("h2", RuntimeError("El disco se corrompió")), ("h3", CancelledError("cancelled"))):
        src = HotSource(_disk(tmp_path), fetch_error=error)
        res, msgs, err = _hot_run(tmp_path, src, name)
        assert err is error and src.calls[-1] == "hot_release"       # released, and the original error still surfaces


def test_failing_to_release_the_checkpoint_is_reported_loudly_but_does_not_hide_the_result(tmp_path):
    src = HotSource(_disk(tmp_path), release_error=RuntimeError("acceso denegado"))
    res, msgs, err = _hot_run(tmp_path, src, "h4")
    assert err is None and res["hot"] is True
    warning = next(m for m in msgs if m.startswith("AVISO: no se pudo eliminar el checkpoint temporal"))
    assert "acceso denegado" in warning and "Administrador de Hyper-V" in warning


def test_a_vm_that_is_already_off_needs_no_checkpoint_and_the_run_is_not_marked_hot(tmp_path):
    src = HotSource(_disk(tmp_path), running=False)
    res, msgs, err = _hot_run(tmp_path, src, "h5")
    assert err is None and res["hot"] is False and "hot_prepare" not in src.calls and "hot_release" not in src.calls


def test_hot_copy_is_refused_for_sources_that_cannot_do_it(tmp_path):
    src = FakeSource(_disk(tmp_path))                                  # supports_hot is False
    res, msgs, err = _hot_run(tmp_path, src, "h6")
    assert isinstance(err, RuntimeError) and "solo está disponible para Hyper-V" in str(err)


def test_existing_checkpoints_block_a_hot_copy_and_leftovers_of_ours_are_called_out(tmp_path):
    src = HotSource(_disk(tmp_path), snapshots=["antes de actualizar", "vm2morpheus-20261031-1010"])
    res, msgs, err = _hot_run(tmp_path, src, "h7")
    text = str(err)
    assert "(antes de actualizar, vm2morpheus-20261031-1010)" in text and "interrumpida" in text
    assert src.calls == []                                             # nothing was touched


def test_running_vm_without_hot_copy_still_asks_to_shut_down_and_mentions_the_alternative(tmp_path):
    src = HotSource(_disk(tmp_path))
    res, msgs, err = _hot_run(tmp_path, src, "h8", {"hot_copy": False})
    assert "Apagar la VM de origen" in str(err) and "Copia en caliente" in str(err)


def test_hot_copy_turns_off_in_place_reading_and_dry_run_explains_the_plan(tmp_path):
    src = HotSource(_disk(tmp_path))
    src.direct = True
    res, msgs, err = _hot_run(tmp_path, src, "h9", dry_run=True)
    assert err is None and src.direct is False and src.calls == []     # a dry run creates no checkpoint
    assert any("se crearía un checkpoint temporal" in m for m in msgs)


def test_gui_passes_the_hot_copy_option_and_warns_about_the_running_source():
    from vm2morpheus import gui
    base = {"vm": "w2k16", "morpheus_url": "https://m", "morpheus_token": "t", "source_type": "hyperv",
            "source_host": "h", "source_user": "u", "source_password": "p"}
    assert gui.make_cfgs({**base, "hot_copy": True})[2]["hot_copy"] is True
    assert gui.make_cfgs(dict(base))[2]["hot_copy"] is False
    page = gui.render_page()
    for needle in ('id="hot_copy"', 'id="hotlbl"', "Copia en caliente: no apagar la VM de origen",
                   "Apaga el origen antes de arrancar la VM de Morpheus"):
        assert needle in page
    job = {"status": "done", "cleanup": {"morpheus": {"url": "https://m"}, "vm": "w", "workdir": "x",
           "result": {"instance": {"id": 1}, "images": [{"id": 2, "name": "n"}], "hot": True}}}
    assert gui.done_info(job)["hot"] is True


def test_form_fields_line_up_whatever_the_label_length():
    from vm2morpheus.gui import render_page
    page = render_page()
    assert "align-items:end" in page.split(".row{", 1)[1].split("}", 1)[0]          # inputs share a baseline per row
    assert "input:not([type=checkbox]),select{height:34px}" in page                 # a select is as tall as an input
    assert "<label>Nombre de la VM en Morpheus</label>" in page                    # no wrapping hint under the label


# ------------------------------------------------------------------ parallel flat-disk download
class RangeServer:
    """Stands in for the datastore browser: serves byte ranges of a payload, optionally flaky or range-less."""

    def __init__(self, payload, per_chunk_delay=0.0, ignore_range=False, flaky_starts=(), always_fail_start=None):
        self.payload, self.delay, self.ignore_range = payload, per_chunk_delay, ignore_range
        self.flaky, self.always_fail = set(flaky_starts), always_fail_start
        self.requests, self.closed = [], 0

    def __call__(self, url, **kw):
        import time as _t
        start, end = (int(x) for x in kw["headers"]["Range"].removeprefix("bytes=").split("-"))
        self.requests.append((start, end))
        if start == self.always_fail:
            raise ConnectionError("connection reset")
        if start in self.flaky:
            self.flaky.discard(start)
            raise ConnectionError("connection reset")
        if self.ignore_range:
            return _ranged_response(200, [self.payload])
        data = self.payload[start:end + 1]
        server = self

        class R(_FakeResponse):
            status_code = 206

            def iter_content(self_inner, size):
                try:
                    for i in range(0, len(data), 1000):
                        if server.delay:
                            _t.sleep(server.delay)
                        yield data[i:i + 1000]
                finally:
                    server.closed += 1
        return R([])


def _parallel(tmp_path, srv, total, streams=4, block=100_000, cb=None, **kw):
    from vm2morpheus.sources.vmware import download_parallel
    seen = []
    out = tmp_path / "disk.img"
    got = download_parallel("https://esxi/x", out, total, streams, False, ("root", "pw"), cb or (lambda d, t: seen.append(d)),
                            getter=srv, block=block, sleep=lambda s: None, **kw)
    return got, out, seen


def test_parallel_download_reassembles_the_file_exactly(tmp_path):
    import os
    payload = os.urandom(1_000_003)                         # not a multiple of the block size
    srv = RangeServer(payload)
    got, out, seen = _parallel(tmp_path, srv, len(payload))
    assert got == len(payload) and out.read_bytes() == payload
    assert len(srv.requests) == -(-len(payload) // 100_000)  # one request per block
    covered = sorted(srv.requests)
    assert covered[0][0] == 0 and covered[-1][1] == len(payload) - 1
    assert all(a[1] + 1 == b[0] for a, b in zip(covered, covered[1:]))  # no gaps, no overlaps
    assert seen[-1] == len(payload)


def test_parallel_download_retries_a_failed_block_and_still_ends_exact(tmp_path):
    import os
    payload = os.urandom(450_000)
    srv = RangeServer(payload, flaky_starts=(200_000,))
    got, out, seen = _parallel(tmp_path, srv, len(payload))
    assert out.read_bytes() == payload and got == len(payload)
    assert srv.requests.count((200_000, 299_999)) == 2       # first attempt failed, second worked
    assert max(seen) <= len(payload)                          # the failed attempt's bytes were not double counted


def test_parallel_download_gives_up_on_a_block_that_keeps_failing(tmp_path):
    srv = RangeServer(b"x" * 300_000, always_fail_start=100_000)
    with pytest.raises(RuntimeError, match="Falló un bloque de la descarga en paralelo tras 3 intentos"):
        _parallel(tmp_path, srv, 300_000)


def test_parallel_download_detects_a_server_without_ranges(tmp_path):
    from vm2morpheus.sources.vmware import RangeNotSupported
    with pytest.raises(RangeNotSupported):
        _parallel(tmp_path, RangeServer(b"x" * 300_000, ignore_range=True), 300_000)


def test_parallel_download_stops_its_workers_on_cancel(tmp_path):
    import time as _t
    from vm2morpheus.util import CancelledError
    srv = RangeServer(b"x" * 4_000_000, per_chunk_delay=0.005)

    def cancel_after_some_progress(done, total):
        if done > 20_000:
            raise CancelledError("cancelled")

    with pytest.raises(CancelledError):
        _parallel(tmp_path, srv, 4_000_000, cb=cancel_after_some_progress)
    _t.sleep(0.3)
    assert srv.closed == len(srv.requests)                    # every open response was closed: nobody keeps reading


def test_several_connections_beat_one_when_each_connection_is_slow(tmp_path):
    import time as _t
    payload = b"z" * 800_000
    times = {}
    for streams in (1, 4):
        srv = RangeServer(payload, per_chunk_delay=0.004)     # a per-connection speed limit, like a throttled host
        t0 = _t.monotonic()
        _parallel(tmp_path, srv, len(payload), streams=streams, block=100_000)
        times[streams] = _t.monotonic() - t0
    assert times[4] < times[1] * 0.6, times


def test_flat_parallel_method_falls_back_to_one_connection_when_ranges_are_refused(tmp_path, monkeypatch):
    from vm2morpheus.sources import vmware
    src = vmware.VMwareSource({"host": "esxi1", "username": "u", "password": "p", "download_method": "flat_parallel"})
    src._find = lambda name: object()
    src._datacenter_path = lambda mo: "ha-datacenter"
    used = []

    def refuse(*a, **k):
        raise vmware.RangeNotSupported("sin Range")

    def single(url, local, cookie, verify, cb, **kw):
        used.append(("single", kw["total"]))
        local.write_bytes(b"\0" * kw["total"])
        return kw["total"]

    monkeypatch.setattr(vmware, "download_parallel", refuse)
    monkeypatch.setattr(vmware, "download_to", single)
    vm = VMInfo("vm1", 1, 1024, "bios", "windows", "off", [DiskInfo("d", 4096, "[ds1] vm1/vm1.vmdk", index=0)])
    files = src.fetch_disks(vm, tmp_path, lambda label: __import__("vm2morpheus.util", fromlist=["Progress"]).Progress(label))
    assert used == [("single", 4096)] and files[0].stat().st_size == 4096


def test_flat_parallel_is_routed_and_offered(tmp_path):
    from vm2morpheus.gui import render_page, source_cfg
    src, calls = _vmware_with_stubs("flat_parallel", lambda: ["export"], lambda: ["flat"])
    assert src.fetch_disks(None, tmp_path, None) == ["flat"] and calls == ["flat-parallel"]
    assert 'value="flat_parallel"' in render_page()
    base = {"source_type": "esxi", "source_host": "h", "source_user": "u", "source_password": "p"}
    assert source_cfg({**base, "vmware_method": "flat_parallel"})["download_method"] == "flat_parallel"


# ------------------------------------------- Hyper-V SMB copy of a disk that a running VM holds open
def _fake_smbclient(monkeypatch, data, open_error=None):
    import io
    import sys
    import types
    calls = []

    def open_file(path, mode="r", **kw):
        calls.append((path, mode, kw))
        if open_error:
            raise open_error
        return io.BytesIO(data)

    fake = types.SimpleNamespace(register_session=lambda host, username, password: None,
                                 stat=lambda p: types.SimpleNamespace(st_size=len(data)), open_file=open_file)
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    return calls


def _frozen_vm():
    return VMInfo("w2k16", 1, 4024, "bios", "windows", "on",
                  [DiskInfo("w2k16.vhdx", 80 << 30, r"E:\VMs\w2k16\Virtual Hard Disks\w2k16.vhdx", index=0)])


def test_the_disk_is_opened_with_sharing_allowed_so_a_running_vm_holding_it_does_not_block_the_copy(tmp_path, monkeypatch):
    data = b"vhdx-bytes" * 5000
    calls = _fake_smbclient(monkeypatch, data)
    src = _hv()
    out = src.fetch_disks(_frozen_vm(), tmp_path, lambda label: __import__("vm2morpheus.util", fromlist=["Progress"]).Progress(label))
    path, mode, kw = calls[0]
    assert path == "\\\\h\\E$\\VMs\\w2k16\\Virtual Hard Disks\\w2k16.vhdx" and mode == "rb"
    assert kw["share_access"] == "rwd"                       # read, write and delete sharing: nobody is locked out
    assert out[0].read_bytes() == data


def test_a_sharing_violation_that_remains_is_explained_in_spanish(tmp_path, monkeypatch):
    err = OSError("[Error 1] [NtStatus 0xc0000043] The process cannot access the file because it is being used by "
                  "another process")
    _fake_smbclient(monkeypatch, b"x", open_error=err)
    with pytest.raises(RuntimeError, match=r"w2k16\.vhdx está abierto por otro proceso.*apaga la VM y migra en frío"):
        _hv().fetch_disks(_frozen_vm(), tmp_path, lambda label: __import__("vm2morpheus.util", fromlist=["Progress"]).Progress(label))


def test_other_smb_errors_are_not_disguised_as_sharing_violations(tmp_path, monkeypatch):
    _fake_smbclient(monkeypatch, b"x", open_error=OSError("[NtStatus 0xc0000022] Access is denied"))
    with pytest.raises(OSError, match="0xc0000022"):
        _hv().fetch_disks(_frozen_vm(), tmp_path, lambda label: __import__("vm2morpheus.util", fromlist=["Progress"]).Progress(label))


# ------------------------------------------ an elevated launch must not end up talking to an old unelevated instance
def test_takeover_decision_table():
    from vm2morpheus.gui import plan_takeover
    idle = {"version": "1.0", "admin": False, "busy": False}
    assert plan_takeover(idle, True, "1.0") == "replace"                       # I am admin, the old one is not
    assert plan_takeover({**idle, "admin": True}, True, "1.0") == "reuse"      # same rights, same version
    assert plan_takeover({**idle, "admin": True}, False, "1.0") == "reuse"     # never swap a stronger one for a weaker
    assert plan_takeover(idle, False, "1.0") == "reuse"
    assert plan_takeover(idle, False, "2.0") == "replace"                      # a stale version is replaced
    assert plan_takeover({**idle, "busy": True}, True, "2.0") == "reuse"       # never interrupt a migration
    assert plan_takeover(None, True, "1.0") == "reuse"                         # unknown (older build): do not guess


def test_info_endpoint_reports_version_rights_and_whether_it_is_busy(monkeypatch):
    import urllib.request
    from vm2morpheus import gui
    monkeypatch.setattr(gui, "is_admin", lambda: True)
    srv = HTTPServer(("127.0.0.1", 0), gui.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{srv.server_port}/info").read())
        assert gui.fetch_info(srv.server_port) == info
    finally:
        srv.shutdown()
    assert info["admin"] is True and info["busy"] is False and info["version"] == gui.__version__ and info["pid"] > 0


def test_fetch_info_returns_none_for_an_instance_that_has_no_info_endpoint():
    from http.server import BaseHTTPRequestHandler
    from vm2morpheus import gui

    class Old(BaseHTTPRequestHandler):                      # what a build older than /info answers
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Old)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert gui.fetch_info(srv.server_port) is None
    finally:
        srv.shutdown()


def test_an_elevated_launch_replaces_the_idle_unelevated_instance_on_the_port(monkeypatch, capsys):
    import socket
    import time as _t
    from vm2morpheus import gui
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # the old instance answers from handler threads (not admin); the new launch runs on the main thread (admin)
    monkeypatch.setattr(gui, "is_admin", lambda: threading.current_thread() is threading.main_thread())
    old = threading.Thread(target=gui.serve, kwargs={"port": port, "open_browser": False}, daemon=True)
    old.start()
    for _ in range(40):
        if gui.fetch_info(port):
            break
        _t.sleep(0.1)
    assert gui.fetch_info(port)["admin"] is False
    threading.Timer(4.0, lambda: gui.STATE["server"].shutdown()).start()      # end the new instance afterwards
    gui.serve(port, open_browser=False)                                       # the elevated launch
    out = capsys.readouterr().out
    assert "no tenía permisos de administrador: se ha cerrado para abrir esta" in out
    assert "Ya hay una instancia" not in out
    old.join(timeout=5)
    assert not old.is_alive()                                                  # the old one really exited


def test_page_shows_the_elevation_status_and_the_preflight_explains_it():
    from vm2morpheus import gui
    page = gui.render_page()
    for needle in ('id="adminbadge"', "Sin permisos de administrador", 'fetch("/info")'):
        assert needle in page
