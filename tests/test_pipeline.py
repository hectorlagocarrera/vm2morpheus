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
        if self.path == "/api/virtual-images/7":
            return self._send({"success": True, "virtualImage": {"id": 7, "status": "Active"}})
        self._send({"success": False, "msg": "nope"}, 404)


class FakeSource(Source):
    def __init__(self, disk):
        self.disk = disk
        self.off = False

    def connect(self): pass

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


def test_gui_template_and_job_log():
    from vm2morpheus.gui import JobLog, build_instance_template
    assert build_instance_template({"layout_id": ""}) is None
    t = build_instance_template({"cloud_id": "1", "group_id": "2", "plan_id": "3", "layout_id": "4", "network_id": "5"})
    assert t["instance"]["layout"] == {"id": 4} and t["networkInterfaces"][0]["network"]["id"] == "5"
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
