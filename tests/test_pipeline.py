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
            if self.reject_sata and any("controllerMountPoint" in v for v in sent.get("volumes", [])):
                return self._send({"success": False, "msg": "bad controller"}, 400)
            self.log.append(("instance", sent))
            return self._send({"success": True, "instance": {"id": 42, "name": "vm1"}})
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
