"""Local web GUI (stdlib only). Binds to 127.0.0.1; credentials are never written to disk."""
import contextlib
import json
import threading
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .morpheus import MorpheusClient
from .pipeline import migrate
from .sources import make_source

JOBS: dict[str, dict] = {}
RUN_LOCK = threading.Lock()


class JobLog:
    """Collects log lines and the live (carriage-return) progress line of a job."""

    def __init__(self):
        self.lines, self.cur = [], ""

    def write(self, s):
        for ch in s:
            if ch == "\r":
                self.cur = ""
            elif ch == "\n":
                if self.cur.strip():
                    self.lines.append(self.cur)
                self.cur = ""
            else:
                self.cur += ch
        return len(s)

    def flush(self):
        pass

    def text(self):
        return "\n".join(self.lines + ([self.cur] if self.cur.strip() else []))


def redact(o):
    """Hide anything that looks like a credential before the JSON is shown (and maybe pasted)."""
    if isinstance(o, dict):
        return {k: "***" if any(w in k.lower() for w in ("password", "secret", "token", "privatekey")) else redact(v)
                for k, v in o.items()}
    if isinstance(o, list):
        return [redact(v) for v in o]
    return o


def build_instance_template(f: dict) -> dict | None:
    """Payload for POST /api/instances from the form fields. UNVERIFIED against a live appliance."""
    if not f.get("layout_id"):
        return None
    payload = {
        "zoneId": int(f["cloud_id"]),
        "instance": {
            "name": "{vm_name}",
            "site": {"id": int(f["group_id"])},
            "instanceType": {"code": f.get("instance_type", "ubuntu")},
            "layout": {"id": int(f["layout_id"])},
            "plan": {"id": int(f["plan_id"])},
        },
        "config": {},
    }
    if f.get("network_id"):
        payload["networkInterfaces"] = [{"network": {"id": str(f["network_id"])}}]
    return payload


def make_cfgs(f: dict):
    for key, label in (("vm", "el nombre de la VM"), ("source_host", "el host de origen"),
                       ("morpheus_url", "la URL de Morpheus")):
        f[key] = (f.get(key) or "").strip()
        if not f[key] and (key != "morpheus_url" or f.get("_needs_morpheus", True)):
            raise ValueError(f"Falta {label}")
    src = {"type": f["source_type"], "host": f["source_host"], "username": f["source_user"],
           "password": f["source_password"], "os_family": f.get("os_family", "other"),
           "verify_ssl": False, "direct_read": bool(f.get("direct_read"))}
    m = {"url": f["morpheus_url"], "token": f["morpheus_token"], "verify_ssl": bool(f.get("verify_ssl")),
         "image_type": f.get("image_type") or "qcow2"}
    tpl = build_instance_template(f)
    if f.get("ref_instance_id"):
        m["reference_instance"] = {"instance_id": int(f["ref_instance_id"]),
                                   "image_id": int(f["ref_image_id"]) if f.get("ref_image_id") else None}
    elif tpl:
        m["instance"] = tpl
    opts = {"workdir": f.get("workdir") or "vm2morpheus-work", "shutdown_source": bool(f.get("shutdown")),
            "inject_drivers": bool(f.get("inject_drivers")), "keep_files": False, "multi_disk_bundle": bool(f.get("multi_disk_bundle"))}
    return src, m, opts


def run_job(job_id: str, f: dict, dry: bool):
    job, jl = JOBS[job_id], JobLog()
    job["jl"] = jl
    with RUN_LOCK, contextlib.redirect_stderr(jl):
        try:
            src_cfg, m, opts = make_cfgs(f)
            source = make_source(src_cfg)
            source.connect()
            try:
                client = MorpheusClient(m["url"], m["token"], verify=m["verify_ssl"])
                migrate(source, client, f["vm"], opts, m, dry_run=dry, log=lambda s: jl.lines.append(str(s)))
            finally:
                source.close()
            job["status"] = "done"
        except Exception as e:  # shown to the user in the log panel
            jl.lines.append(f"ERROR: {e}")
            job["status"] = "error"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            b = PAGE.replace("__VERSION__", __version__).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path.startswith("/job/"):
            job = JOBS.get(self.path[5:])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            jl = job.get("jl")
            self._json({"status": job["status"], "log": jl.text() if jl else ""})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        f = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/lookups":
                c = MorpheusClient(f["morpheus_url"], f["morpheus_token"], verify=bool(f.get("verify_ssl")))
                zone = f.get("cloud_id")
                plans = c._req("GET", "/api/service-plans", params={"max": 200, **({"zoneId": zone} if zone else {})})
                return self._json({
                    "clouds": [(z["id"], z["name"]) for z in c.clouds()],
                    "groups": [(g["id"], g["name"]) for g in c._req("GET", "/api/groups", params={"max": 200}).get("groups", [])],
                    "plans": [(p["id"], p["name"]) for p in plans.get("servicePlans", [])],
                    "instances": [(i["id"], i["name"]) for i in c._req("GET", "/api/instances", params={"max": 200}).get("instances", [])],
                    "networks": [(n["id"], n["name"]) for n in c._req("GET", "/api/networks", params={"max": 200}).get("networks", [])],
                })
            if self.path == "/refinfo":
                c = MorpheusClient(f["morpheus_url"], f["morpheus_token"], verify=bool(f.get("verify_ssl")))
                if not f.get("ref_instance_id"):
                    raise ValueError("Elige primero la instancia de referencia")
                return self._json(redact(c.get_instance(int(f["ref_instance_id"]))))
            if self.path == "/inspect":
                src_cfg, _, _ = make_cfgs(f)
                s = make_source(src_cfg)
                s.connect()
                try:
                    return self._json(asdict(s.get_vm(f["vm"])))
                finally:
                    s.close()
            if self.path in ("/migrate", "/dryrun"):
                if RUN_LOCK.locked():
                    return self._json({"error": "another job is running"}, 409)
                jid = uuid.uuid4().hex[:8]
                JOBS[jid] = {"status": "running"}
                threading.Thread(target=run_job, args=(jid, f, self.path == "/dryrun"), daemon=True).start()
                return self._json({"job": jid})
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)


def serve(port: int = 8765, open_browser: bool = True):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"vm2morpheus GUI on {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>vm2morpheus</title>
<style>
body{font:15px system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1b2430}
main{max-width:920px;margin:0 auto;padding:16px}
h1{font-size:22px} fieldset{background:#fff;border:1px solid #d5dbe2;border-radius:8px;margin:0 0 14px;padding:12px 16px}
legend{font-weight:600;padding:0 6px} label{display:block;margin:8px 0 2px;font-size:13px;color:#4a5665}
input,select{width:100%;padding:7px;border:1px solid #b9c2cc;border-radius:5px;box-sizing:border-box}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 14px}
.chk label{display:inline-block;margin-right:16px;color:#1b2430;font-size:14px} .chk input{width:auto}
button{padding:9px 16px;margin:4px 8px 4px 0;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer;font-size:14px}
button.sec{background:#e5e9ee;color:#1b2430} pre{background:#0f172a;color:#d6e2f0;padding:12px;border-radius:8px;min-height:140px;max-height:420px;overflow:auto;white-space:pre-wrap}
small{color:#5d6b7b}
</style><main>
<h1>vm2morpheus <small>Hyper-V / VMware → Morpheus (KVM) · v__VERSION__</small></h1>
<fieldset><legend>1. Origen</legend><div class="row">
<div><label>Tipo</label><select id="source_type"><option value="hyperv">Hyper-V</option><option value="vmware">VMware (vCenter/ESXi)</option></select></div>
<div><label>Host / IP</label><input id="source_host"></div>
<div><label>Usuario</label><input id="source_user"></div>
<div><label>Contraseña</label><input id="source_password" type="password"></div>
<div><label>Nombre de la VM</label><input id="vm"></div>
<div><label>Sistema operativo (Hyper-V)</label><select id="os_family"><option value="windows">Windows</option><option value="linux">Linux</option><option value="other">Otro</option></select></div>
</div><button class="sec" onclick="inspect_()">Inspeccionar VM</button></fieldset>
<fieldset><legend>2. Destino Morpheus</legend><div class="row">
<div><label>URL (https://ip)</label><input id="morpheus_url"></div>
<div><label>Token API</label><input id="morpheus_token" type="password"></div>
<div><label>imageType <small>(qcow2 por defecto, verifícalo)</small></label><input id="image_type" value="qcow2"></div>
</div><div class="chk"><label><input type="checkbox" id="verify_ssl"> Verificar certificado TLS</label></div>
<button class="sec" onclick="lookups()">Cargar nubes, grupos, planes y redes</button></fieldset>
<fieldset><legend>3. Crear la VM en Morpheus (opcional)</legend>
<small>Crea antes a mano UNA instancia HPE HVM con cualquier imagen (Provisioning → Instances → Add). Elígela en el desplegable: el programa la clona cambiando solo la imagen. Vacío = solo se sube la imagen.</small>
<div class="row"><div><label>Instancia de referencia (pulsa "Cargar" arriba)</label><select id="ref_instance_id"><option value="">(ninguna: solo subir la imagen)</option></select></div>
<div><label>ID de su imagen <small>(opcional; se detecta sola)</small></label><input id="ref_image_id"></div></div>
<button class="sec" onclick="refinfo()">Ver instancia de referencia (JSON)</button>
<details><summary><small>Alternativa avanzada: layout manual</small></summary>
<div class="row"><div><label>Nube</label><select id="cloud_id"></select></div><div><label>Grupo</label><select id="group_id"></select></div>
<div><label>Plan</label><select id="plan_id"></select></div><div><label>Red</label><select id="network_id"></select></div>
<div><label>Instance type (código)</label><input id="instance_type" value="ubuntu"></div><div><label>Layout ID</label><input id="layout_id"></div></div></details></fieldset>
<fieldset><legend>4. Opciones</legend><div class="chk">
<label><input type="checkbox" id="shutdown"> Apagar la VM de origen</label>
<label><input type="checkbox" id="multi_disk_bundle" checked> Varios discos en una sola imagen (zip + metadata.json)</label>
<label><input type="checkbox" id="direct_read"> Conversión directa, sin copiar el disco antes (Hyper-V, solo Windows)</label>
<label><input type="checkbox" id="inject_drivers"> Inyectar drivers VirtIO (experimental)</label></div>
<label>Carpeta de trabajo (necesita ~2× el tamaño de los discos)</label><input id="workdir" value="vm2morpheus-work"></fieldset>
<button onclick="run('/dryrun')" class="sec">Simulación</button><button onclick="run('/migrate')">Migrar</button>
<pre id="log">Listo.</pre></main>
<script>
const ids=["source_type","source_host","source_user","source_password","vm","os_family","morpheus_url","morpheus_token","image_type","cloud_id","group_id","plan_id","network_id","instance_type","layout_id","ref_instance_id","ref_image_id","workdir"];
const chks=["verify_ssl","shutdown","direct_read","multi_disk_bundle","inject_drivers"];
const form=()=>{const o={};ids.forEach(i=>o[i]=document.getElementById(i).value);chks.forEach(i=>o[i]=document.getElementById(i).checked);return o};
const show=t=>document.getElementById("log").textContent=t;
async function post(p,b){const r=await fetch(p,{method:"POST",body:JSON.stringify(b)});const j=await r.json();if(j.error)throw new Error(j.error);return j}
async function inspect_(){try{show(JSON.stringify(await post("/inspect",form()),null,2))}catch(e){show("ERROR: "+e.message)}}
async function refinfo(){try{show(JSON.stringify(await post("/refinfo",form()),null,2))}catch(e){show("ERROR: "+e.message)}}
async function lookups(){try{const j=await post("/lookups",form());
 for(const [k,id] of [["clouds","cloud_id"],["groups","group_id"],["plans","plan_id"],["networks","network_id"],["instances","ref_instance_id"]]){
  const s=document.getElementById(id);s.innerHTML=(id=="network_id"||id=="ref_instance_id"?'<option value="">(ninguna)</option>':'')+j[k].map(([v,n])=>`<option value="${v}">${n}</option>`).join("")}
 show("Datos cargados.")}catch(e){show("ERROR: "+e.message)}}
async function run(p){try{const {job}=await post(p,form());show("Trabajo "+job+" en marcha...");
 const t=setInterval(async()=>{const r=await (await fetch("/job/"+job)).json();show(r.log||"...");if(r.status!=="running")clearInterval(t)},1000)}catch(e){show("ERROR: "+e.message)}}
</script></html>"""
