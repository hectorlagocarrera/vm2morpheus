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
    reject_sata = False

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
