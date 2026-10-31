"""Local web GUI (stdlib only). Binds to 127.0.0.1; credentials are never written to disk."""
import contextlib
import json
import time
import urllib.parse
import threading
import uuid
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, settings
from .logo import LOGO_PNG
from .morpheus import MorpheusClient
from .pipeline import migrate
from .sources import make_source
from .util import CancelledError, Progress, browse, pick_workdir, redact

STATE = {"server": None, "last_ping": None}
IDLE_LIMIT = 180  # seconds without the page pinging us (tab closed) before the app exits
JOBS: dict[str, dict] = {}
RUN_LOCK = threading.Lock()


def idle_too_long(last_ping, now: float, busy: bool, limit: float = IDLE_LIMIT) -> bool:
    """True when the browser page has gone away (no heartbeat) and no migration is running."""
    return last_ping is not None and not busy and now - last_ping > limit


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


def synthetic_reference(f: dict) -> dict:
    """An instance description shaped like what GET /api/instances/{id} returns for an HVM VM,
    built from the form so no hand-made reference instance is needed. Defaults (storageType 1,
    datastore "auto", poolProviderType "mvm") were taken from a real HVM instance of Morpheus."""
    net = f.get("network_id")
    return {
        "cloud": {"id": int(f["cloud_id"])},
        "group": {"id": int(f["group_id"])},
        "instanceType": {"code": f.get("instance_type") or "mvm"},
        "layout": {"id": int(f["layout_id"]) if f.get("layout_id") else None},  # None = detected at run time
        "plan": {"id": int(f["plan_id"])},
        "config": {"imageId": None, "resourcePoolId": f.get("resource_pool") or None,
                   "poolProviderType": "mvm", "layoutSize": 1, "createUser": False, "uefi": False},
        "volumes": [{"name": "root", "rootVolume": True, "size": 1, "storageType": 1, "datastoreId": "auto"}],
        "interfaces": [{"id": f"network-{net}", "network": {"id": int(net)}}] if net else [],
    }


def _need(f: dict, key: str, label: str) -> None:
    f[key] = (f.get(key) or "").strip()
    if not f[key]:
        raise ValueError(f"Falta {label}")


def source_cfg(f: dict) -> dict:
    _need(f, "source_host", "el host de origen")
    _need(f, "source_user", "el usuario del origen")
    return {"type": f["source_type"], "host": f["source_host"], "username": f["source_user"],
            "password": f["source_password"], "os_family": f.get("os_family", "other"),
            "verify_ssl": bool(f.get("verify_source")), "direct_read": bool(f.get("direct_read")),
            "download_method": f.get("vmware_method") or "auto"}


def make_cfgs(f: dict):
    _need(f, "vm", "el nombre de la VM")
    _need(f, "morpheus_url", "la URL de Morpheus")
    src = source_cfg(f)
    m = {"url": f["morpheus_url"], "token": f["morpheus_token"], "verify_ssl": bool(f.get("verify_ssl")),
         "image_type": f.get("image_type") or "qcow2"}
    if f.get("ref_instance_id"):
        m["reference_instance"] = {"instance_id": int(f["ref_instance_id"]),
                                   "image_id": int(f["ref_image_id"]) if f.get("ref_image_id") else None}
    elif f.get("cloud_id") and f.get("group_id") and f.get("plan_id"):
        if not f.get("network_id"):  # Morpheus refuses a VM without a network, and it would only say so at the very end
            raise ValueError("Elige la red de la VM nueva (bloque 3, campo Red). Morpheus la exige, y sin ella la "
                             "migración fallaría al final, después de descargar y subir el disco.")
        m["reference_instance"] = {"synthetic": synthetic_reference(f)}
    opts = {"workdir": resolve_workdir(f), "shutdown_source": bool(f.get("shutdown")),
            "boot_sata": bool(f.get("boot_sata")), "inject_drivers": bool(f.get("inject_drivers")),
            "virtio_iso": (f.get("virtio_iso") or "").strip() or None, "keep_files": False, "multi_disk_bundle": bool(f.get("multi_disk_bundle")),
            "recreate_instance": bool(f.get("recreate_instance")),
            "disk_indexes": [int(i) for i in f["disk_indexes"]] if f.get("disk_indexes") is not None else None,
            "new_name": (f.get("new_vm_name") or "").strip() or None,
            "compress_image": bool(f.get("compress_image"))}
    return src, m, opts


def resolve_workdir(f: dict) -> str:
    """The typed folder, else one chosen automatically (and remembered so a resume finds its files)."""
    wd = (f.get("workdir") or "").strip()
    if wd:
        return wd
    saved = settings.load().get("auto_workdir")
    if saved:
        return saved
    wd = pick_workdir()
    data = settings.load()
    data["auto_workdir"] = wd
    settings.save(data)
    return wd


def run_job(job_id: str, f: dict, dry: bool):
    job, jl = JOBS[job_id], JobLog()
    job["jl"] = jl
    Progress.observer = lambda label, done, total, unit: job.__setitem__(
        "transfer", {"label": label, "done": done, "total": total, "unit": unit, "t": time.time()})
    with RUN_LOCK, contextlib.redirect_stderr(jl):
        try:
            src_cfg, m, opts = make_cfgs(f)
            source = make_source(src_cfg)
            source.connect()
            try:
                client = MorpheusClient(m["url"], m["token"], verify=m["verify_ssl"])
                migrate(source, client, f["vm"], opts, m, dry_run=dry, log=lambda s: jl.lines.append(str(s)),
                        cancel=job["cancel"], on_progress=lambda p: job.__setitem__("progress", p))
            finally:
                source.close()
            job["status"] = "done"
        except CancelledError:
            jl.lines.append("Migración cancelada. Lo ya descargado o convertido se conserva: al volver a pulsar "
                            "Migrar se reanuda desde ahí.")
            job["status"] = "cancelled"
        except Exception as e:  # shown to the user in the log panel
            jl.lines.append(f"ERROR: {e}")
            job["status"] = "error"
        finally:
            Progress.observer = None


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
            b = render_page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/ping":
            STATE["last_ping"] = time.monotonic()
            self._json({"ok": True})
        elif self.path == "/logo.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(LOGO_PNG)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(LOGO_PNG)
        elif self.path.startswith("/browse"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._json(browse(query.get("path", [""])[0]))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif self.path == "/settings":
            self._json(settings.load())
        elif self.path.startswith("/job/"):
            job = JOBS.get(self.path[5:])
            if not job:
                return self._json({"error": "unknown job"}, 404)
            jl = job.get("jl")
            self._json({"status": job["status"], "log": jl.text() if jl else "", "progress": job.get("progress"),
                        "transfer": job.get("transfer")})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        f = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/lookups":
                c = MorpheusClient(f["morpheus_url"], f["morpheus_token"], verify=bool(f.get("verify_ssl")))
                zone = f.get("cloud_id")
                plans = c._req("GET", "/api/service-plans", params={"max": 200, **({"zoneId": zone} if zone else {})})
                layouts = c.layouts("mvm")
                pools = c.resource_pools(int(zone)) if zone else []
                return self._json({
                    "layouts": layouts, "pools": pools,
                    "clouds": [(z["id"], z["name"]) for z in c.clouds()],
                    "groups": [(g["id"], g["name"]) for g in c._req("GET", "/api/groups", params={"max": 200}).get("groups", [])],
                    "plans": [(p["id"], p["name"], p.get("maxCores"), p.get("maxMemory")) for p in plans.get("servicePlans", [])
                              if "discover" not in f'{p.get("code", "")} {p.get("name", "")}'.lower()],
                    "instances": [(i["id"], i["name"]) for i in c._req("GET", "/api/instances", params={"max": 200}).get("instances", [])],
                    "networks": [(n["id"], n["name"]) for n in c._req("GET", "/api/networks", params={"max": 200}).get("networks", [])],
                })
            if self.path == "/refinfo":
                c = MorpheusClient(f["morpheus_url"], f["morpheus_token"], verify=bool(f.get("verify_ssl")))
                if not f.get("ref_instance_id"):
                    raise ValueError("Elige primero la instancia de referencia")
                return self._json(redact(c.get_instance(int(f["ref_instance_id"]))))
            if self.path == "/listvms":
                s = make_source(source_cfg(f))
                s.connect()
                try:
                    kind = s.kind() if hasattr(s, "kind") else "Hyper-V"
                    return self._json({"vms": s.list_vms(), "kind": kind})
                finally:
                    s.close()
            if self.path == "/shutdown":
                threading.Timer(0.3, STATE["server"].shutdown).start()  # after this response has been sent
                return self._json({"ok": True})
            if self.path == "/cancel":
                job = JOBS.get(f.get("job"))
                if not job:
                    raise ValueError("trabajo desconocido")
                job["cancel"].set()
                return self._json({"ok": True})
            if self.path == "/settings":
                settings.save(f)
                return self._json({"ok": True})
            if self.path == "/speedtest":
                _need(f, "vm", "el nombre de la VM")
                s = make_source(source_cfg(f))
                if not hasattr(s, "speed_test"):
                    raise ValueError("La prueba de velocidad solo está disponible para VMware (vCenter o ESXi)")
                s.connect()
                try:
                    return self._json(s.speed_test(f["vm"]))
                finally:
                    s.close()
            if self.path == "/inspect":
                _need(f, "vm", "el nombre de la VM")
                s = make_source(source_cfg(f))
                s.connect()
                try:
                    return self._json(asdict(s.get_vm(f["vm"])))
                finally:
                    s.close()
            if self.path in ("/migrate", "/dryrun"):
                if RUN_LOCK.locked():
                    return self._json({"error": "another job is running"}, 409)
                jid = uuid.uuid4().hex[:8]
                JOBS[jid] = {"status": "running", "cancel": threading.Event()}
                threading.Thread(target=run_job, args=(jid, f, self.path == "/dryrun"), daemon=True).start()
                return self._json({"job": jid})
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "not found"}, 404)


def serve(port: int = 8765, open_browser: bool = True, embedded: bool = False):
    """Run the GUI in the default browser (the app exits when the tab goes away). With embedded=True it opens its
    own window instead (needs pywebview + WebView2; not part of the packaged exe) and falls back to the browser."""
    url = f"http://127.0.0.1:{port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:  # a second launch: do not start another server
        print(f"Ya hay una instancia en marcha en {url}: se abre en el navegador")
        if open_browser:
            if embedded:
                from .launcher import show_info
                show_info("vm2morpheus ya está abierto: búscalo en la barra de tareas.")
            else:
                webbrowser.open(url)
        return
    STATE["server"] = srv
    STATE["embedded"] = False
    print(f"Interfaz de vm2morpheus en {url}  (Ctrl+C para parar)")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    if open_browser and embedded:
        from .window import open_window
        STATE["embedded"] = True  # tells the page to hide its own "Salir" button
        if open_window(url, RUN_LOCK.locked):
            srv.shutdown()
            return
        STATE["embedded"] = False
        print("No se pudo abrir la ventana integrada (falta WebView2 o pywebview): se usa el navegador")

    def watchdog():
        while thread.is_alive():
            time.sleep(10)
            if idle_too_long(STATE["last_ping"], time.monotonic(), RUN_LOCK.locked()):
                srv.shutdown()
                return

    threading.Thread(target=watchdog, daemon=True).start()
    if open_browser:
        webbrowser.open(url)
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        srv.shutdown()


def render_page() -> str:
    return PAGE.replace("__VERSION__", __version__).replace("__EMBEDDED__", "true" if STATE.get("embedded") else "false")


PAGE = r"""<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>vm2morpheus</title>
<link rel="icon" href="/logo.png">
<style>
body{font:15px system-ui,sans-serif;margin:0;background:#f4f6f8;color:#1b2430}
main{max-width:920px;margin:0 auto;padding:16px}
.brand{padding:4px 0 10px;display:flex;justify-content:space-between;align-items:center} .brand .quit{margin:0} .brand img{height:56px;max-width:100%}
h1{font-size:22px} fieldset{background:#fff;border:1px solid #d5dbe2;border-radius:8px;margin:0 0 14px;padding:12px 16px}
legend{font-weight:600;padding:0 6px} label{display:block;margin:8px 0 2px;font-size:13px;color:#4a5665}
input,select{width:100%;padding:7px;border:1px solid #b9c2cc;border-radius:5px;box-sizing:border-box}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:0 14px}
.chk label{display:inline-block;margin-right:16px;color:#1b2430;font-size:14px} .chk input{width:auto}
button{padding:9px 16px;margin:4px 8px 4px 0;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer;font-size:14px}
input.diskchk{width:auto;margin:0 8px 0 0;vertical-align:middle}
#picker{display:none;background:#fff;border:1px solid #b9c2cc;border-radius:8px;padding:10px;margin-top:8px}
#picker .item{padding:5px 8px;cursor:pointer;border-radius:4px} #picker .item:hover{background:#e8eef7}
#pickerlist{max-height:220px;overflow:auto;border:1px solid #e1e6ec;border-radius:6px;margin:6px 0}
#busywrap{display:none;margin:0 0 10px;position:sticky;top:0;z-index:5;background:#f4f6f8;padding:8px 0}
#busyline{height:8px;background:#dfe5ec;border-radius:6px;overflow:hidden;position:relative}
#busyline::before{content:"";position:absolute;top:0;bottom:0;width:40%;background:#2563eb;border-radius:6px;animation:slide 1.1s infinite ease-in-out}
@keyframes slide{0%{left:-40%}100%{left:100%}}
#busytxt{font-size:13px;margin-top:4px;color:#4a5665}
button:disabled{opacity:.55;cursor:wait}
#barwrap{display:none;margin:10px 0}
#barline{background:#dfe5ec;border-radius:6px;height:20px;overflow:hidden}
#bar{height:100%;width:0;background:#2563eb;transition:width .4s}
#bar.ok{background:#1e8e4e} #bar.err{background:#c0392b} #bar.stop{background:#7b8794}
#barlabel{font-size:13px;margin-top:4px;color:#4a5665}
button.danger{background:#c0392b}
button.sec{background:#e5e9ee;color:#1b2430} .logbar{display:flex;justify-content:space-between;align-items:center;margin:14px 0 6px}
.logbar b{font-size:14px} .logbar button.mini{padding:4px 10px;margin:0 0 0 6px;font-size:12px}
.logbox{background:#0b1220;color:#d6e2f0;border-radius:10px;padding:10px 0;min-height:140px;max-height:440px;overflow:auto;
 font:13px/1.5 Consolas,"Cascadia Mono",Menlo,monospace;user-select:text;-webkit-user-select:text;cursor:text;box-shadow:inset 0 0 0 1px #1e293b}
.logbox .ln{padding:0 14px;white-space:pre-wrap;word-break:break-word;border-left:3px solid transparent}
.logbox .ln:hover{background:#111b30}
.logbox .step{color:#93c5fd;font-weight:600;border-left-color:#3b82f6;margin-top:4px}
.logbox .prog{color:#9fb3c8} .logbox .ok{color:#86efac;border-left-color:#22c55e}
.logbox .warn{color:#fcd34d;border-left-color:#f59e0b} .logbox .err{color:#fca5a5;font-weight:600;border-left-color:#ef4444;background:#2a1215}
#chartwrap{display:none;margin:12px 0}
#chartwrap .head{display:flex;justify-content:space-between;align-items:baseline;font-size:13px;margin-bottom:4px}
#chartwrap .head b{font-size:14px} #chartinfo{color:#4a5665}
#speedchart{width:100%;height:170px;background:#fff;border:1px solid #d5dbe2;border-radius:10px;display:block}
#chartlegend{font-size:12px;color:#4a5665;margin-top:4px}
#chartlegend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 12px;vertical-align:middle}
small{color:#5d6b7b}
</style><main>
<div class="brand"><img src="/logo.png" alt="Redes · 25 años innovando el futuro"><button class="sec quit" onclick="quit()">Salir del programa</button></div>
<h1>vm2morpheus <small>Hyper-V / VMware → Morpheus (KVM) · v__VERSION__</small></h1>
<div id="busywrap"><div id="busyline"></div><div id="busytxt"></div></div>
<fieldset><legend>1. Origen</legend><div class="row">
<div><label>Tipo</label><select id="source_type" onchange="srcHint()"><option value="hyperv">Hyper-V</option><option value="vmware">VMware vCenter</option><option value="esxi">VMware ESXi (sin vCenter)</option></select></div>
<div><label>Host / IP</label><input id="source_host"></div>
<div><label>Usuario</label><input id="source_user" placeholder="Administrador"></div>
<div><label>Contraseña</label><input id="source_password" type="password"></div>
<div><label>Nombre de la VM</label><input id="vm" onchange="analyze()"></div>
<div><label>VM del origen <small>(pulsa "Listar VMs")</small></label><select id="vm_select" onchange="if(this.value){document.getElementById('vm').value=this.value;analyze()}"><option value="">(sin listar)</option></select></div>
<div><label>Sistema operativo (Hyper-V)</label><select id="os_family"><option value="windows">Windows</option><option value="linux">Linux</option><option value="other">Otro</option></select></div>
</div><button class="sec" data-busy onclick="listvms()">Listar VMs del origen</button><button class="sec" data-busy onclick="inspect_()">Analizar VM</button><button class="sec" data-busy onclick="speedtest()">Probar velocidad de descarga</button>
<div id="diskpanel" style="display:none;margin-top:8px"><div id="diskinfo"></div><div id="disklist" style="margin:6px 0"></div><small id="diskhint"></small></div></fieldset>
<fieldset><legend>2. Destino Morpheus</legend><div class="row">
<div><label>URL (https://ip)</label><input id="morpheus_url"></div>
<div><label>Nombre de la VM en Morpheus <small>(igual que la original si lo dejas)</small></label><input id="new_vm_name" oninput="newNameTouched=true" placeholder="igual que la original"></div>
<div><label>Token API</label><input id="morpheus_token" type="password"></div>
<div><label>imageType <small>(qcow2 por defecto, verifícalo)</small></label><input id="image_type" value="qcow2"></div>
</div><div class="chk"><label><input type="checkbox" id="verify_ssl"> Verificar certificado TLS</label></div>
<button class="sec" data-busy onclick="lookups()">Cargar nubes, grupos, planes y redes</button></fieldset>
<fieldset><legend>3. Crear la VM en Morpheus (opcional)</legend>
<small>Crea antes a mano UNA instancia HPE HVM con cualquier imagen (Provisioning → Instances → Add). Elígela en el desplegable: el programa la clona cambiando solo la imagen. Vacío = solo se sube la imagen.</small>
<div class="row"><div><label>Instancia de referencia (pulsa "Cargar" arriba)</label><select id="ref_instance_id"><option value="">(ninguna: solo subir la imagen)</option></select></div>
<div><label>ID de su imagen <small>(opcional; se detecta sola)</small></label><input id="ref_image_id"></div></div>
<button class="sec" data-busy onclick="refinfo()">Ver instancia de referencia (JSON)</button>
<details open><summary><small>Sin instancia de referencia: crear desde cero (rellena nube, grupo, plan, red y layout)</small></summary>
<div class="row"><div><label>Nube</label><select id="cloud_id"></select></div><div><label>Grupo</label><select id="group_id"></select></div>
<div><label>Plan <small id="planhint"></small></label><select id="plan_id" onchange="planTouched=true"></select></div><div><label>Red <small>(obligatoria)</small></label><select id="network_id"></select></div>
<div><label>Instance type (código)</label><input id="instance_type" value="mvm"></div><div><label>Layout <small>(automático: Single HVM)</small></label><select id="layout_id"><option value="">(automático)</option></select></div><div><label>Resource pool <small>(automático)</small></label><select id="resource_pool"><option value="">(automático)</option></select></div></div></details></fieldset>
<fieldset><legend>4. Opciones</legend><div class="chk">
<label><input type="checkbox" id="shutdown"> Apagar la VM de origen</label>
<label><input type="checkbox" id="multi_disk_bundle" checked> Varios discos en una sola imagen (zip + metadata.json)</label>
<label><input type="checkbox" id="recreate_instance"> Recrear solo la instancia (borra antes la anterior en Morpheus)</label>
<label><input type="checkbox" id="verify_source"> Verificar el certificado del origen (VMware; déjalo apagado si es autofirmado)</label>
<label><input type="checkbox" id="compress_image"> Comprimir el disco convertido (se sube y se copia más rápido; la VM puede leer algo más lento)</label>
<label><input type="checkbox" id="remember_secrets"> Recordar contraseñas y token (se guardan en texto plano en este equipo)</label>
<label><input type="checkbox" id="direct_read"> Conversión directa, sin copiar el disco antes (Hyper-V, solo Windows)</label>
<label><input type="checkbox" id="inject_drivers"> Inyectar drivers VirtIO en el disco de Windows (solo Windows; ejecuta el programa como administrador)</label>
<label><input type="checkbox" id="boot_sata" checked> Disco de arranque en SATA (Windows sin drivers VirtIO)</label></div>
<label>ISO de virtio-win <small>(vacío = se descarga sola, ~700 MB, y se guarda en caché)</small></label><input id="virtio_iso" placeholder="C:\ISOs\virtio-win.iso">
<label>Descarga desde VMware <small>(Automático: exportación comprimida y, si se para, disco plano)</small></label><select id="vmware_method"><option value="auto">Automático</option><option value="export">Solo exportación (comprimida, más rápida)</option><option value="flat">Disco plano (sin comprimir, más lenta pero fiable)</option></select>
<label>Carpeta de trabajo <small>(necesita ~2× el tamaño real de los discos; vacío = automática)</small></label><div style="display:flex;gap:8px"><input id="workdir" placeholder="vacío = automático (disco local con más espacio libre)"><button class="sec" style="margin:0;white-space:nowrap" onclick="openPicker(document.getElementById('workdir').value.trim())">Examinar…</button></div>
<div id="picker"><div id="pickerhead"></div><div id="pickerlist"></div>
<button class="sec" onclick="pickerUp()">Subir</button><button onclick="pickerUse()">Usar esta carpeta</button><button class="sec" onclick="closePicker()">Cerrar</button></div></fieldset>
<button onclick="run('/dryrun')" class="sec">Simulación</button><button onclick="run('/migrate')">Migrar</button><button id="cancelbtn" class="danger" style="display:none" onclick="cancelJob()">Cancelar migración</button>
<div id="barwrap"><div id="barline"><div id="bar"></div></div><div id="barlabel"></div></div>
<div id="chartwrap"><div class="head"><b>Transferencia en directo</b><span id="chartinfo"></span></div>
<canvas id="speedchart"></canvas>
<div id="chartlegend"><i style="background:#2563eb"></i>Descarga<i style="background:#16a34a"></i>Subida a Morpheus</div></div>
<div class="logbar"><b>Registro</b><span><button class="sec mini" onclick="copyLog()">Copiar</button><button class="sec mini" onclick="saveLog()">Guardar .txt</button><button class="sec mini" onclick="clearLog()">Limpiar</button></span></div>
<div id="log" class="logbox" tabindex="0">Listo.</div></main>
<script>
const ids=["source_type","source_host","source_user","source_password","vm","os_family","morpheus_url","morpheus_token","image_type","cloud_id","group_id","plan_id","network_id","instance_type","layout_id","resource_pool","virtio_iso","vmware_method","new_vm_name","ref_instance_id","ref_image_id","workdir"];
const chks=["verify_ssl","verify_source","compress_image","remember_secrets","shutdown","direct_read","multi_disk_bundle","recreate_instance","inject_drivers","boot_sata"];
const EMBEDDED=__EMBEDDED__;
let analyzedVm=null;
const form=()=>{const o={};ids.forEach(i=>o[i]=document.getElementById(i).value);chks.forEach(i=>o[i]=document.getElementById(i).checked);if(analyzedVm&&analyzedVm===document.getElementById("vm").value.trim()&&document.querySelector(".diskchk"))o.disk_indexes=[...document.querySelectorAll(".diskchk")].filter(c=>c.checked).map(c=>Number(c.value));else o.disk_indexes=null;return o};
let shown=[];
function lineClass(l){
 if(/^ERROR/.test(l))return"err";
 if(/^(Aviso|Nota|Advertencia)/i.test(l))return"warn";
 if(/terminado|^Terminado|Conectado a|encontradas|ya se migró|reutilizan/i.test(l))return"ok";
 if(/^\s+\S.*(MiB|GiB|KiB|%)/.test(l))return"prog";
 if(/^(Descargando|Convirtiendo|Subiendo|Creando|Inyectando|Empaquetando|Apagando|Añadiendo|Quitando|Simulación|Se contin)/.test(l))return"step";
 return""}
// Only the lines that changed are touched, so selecting text while a migration runs no longer gets wiped every second.
function show(text){const box=document.getElementById("log"),lines=String(text).split("\n");
 const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<40;
 let i=0;while(i<lines.length&&i<shown.length&&lines[i]===shown[i])i++;
 if(!shown.length)box.textContent="";
 while(box.children.length>i)box.removeChild(box.lastChild);
 for(let k=i;k<lines.length;k++){const d=document.createElement("div");d.className="ln "+lineClass(lines[k]);d.textContent=lines[k]||" ";box.appendChild(d)}
 shown=lines;if(atBottom)box.scrollTop=box.scrollHeight}
async function copyLog(){const text=shown.join("\n");
 try{await navigator.clipboard.writeText(text)}catch(e){const a=document.createElement("textarea");a.value=text;document.body.appendChild(a);a.select();document.execCommand("copy");a.remove()}
 const b=document.querySelector(".logbar button");const o=b.textContent;b.textContent="¡Copiado!";setTimeout(()=>b.textContent=o,1200)}
function saveLog(){const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([shown.join("\n")],{type:"text/plain"}));
 a.download="vm2morpheus-"+new Date().toISOString().slice(0,19).replace(/[:T]/g,"-")+".txt";a.click();URL.revokeObjectURL(a.href)}
function clearLog(){shown=[];document.getElementById("log").textContent=""}
// ---- live throughput chart (bytes transfers only; conversions have no byte rate)
let series=[],lastT=null,lastDone=0,lastLabel=null,t0=null;
function resetChart(){series=[];lastT=null;lastLabel=null;t0=null;document.getElementById("chartwrap").style.display="none"}
function feedChart(r){const t=r&&r.transfer;if(!t||t.unit!=="bytes")return;
 if(t0===null)t0=t.t;
 if(lastLabel===t.label&&lastT!==null&&t.t>lastT){
  series.push({x:t.t-t0,y:Math.max(0,(t.done-lastDone)/(t.t-lastT)/1048576),up:/^subir/.test(t.label)});
  lastT=t.t;lastDone=t.done}
 else if(lastLabel!==t.label||lastT===null){lastLabel=t.label;lastT=t.t;lastDone=t.done}
 drawChart()}
function niceMax(v){const e=Math.pow(10,Math.floor(Math.log10(v))),f=v/e;return(f<=1?1:f<=2?2:f<=5?5:10)*e}
function fmtSecs(s){s=Math.round(s);return s<60?s+" s":Math.floor(s/60)+" min "+String(s%60).padStart(2,"0")+" s"}
function drawChart(){const wrap=document.getElementById("chartwrap");if(!series.length)return;wrap.style.display="block";
 const c=document.getElementById("speedchart"),dpr=window.devicePixelRatio||1,W=c.clientWidth,H=c.clientHeight;
 c.width=W*dpr;c.height=H*dpr;const x=c.getContext("2d");x.scale(dpr,dpr);x.clearRect(0,0,W,H);
 const L=48,R=10,T=10,B=22,pw=W-L-R,ph=H-T-B;
 const maxX=Math.max(10,series[series.length-1].x),maxY=niceMax(Math.max(1,...series.map(p=>p.y))*1.1);
 const px=v=>L+v/maxX*pw,py=v=>T+ph-v/maxY*ph;
 x.font="11px system-ui,sans-serif";x.fillStyle="#6b7788";x.strokeStyle="#e5e9ee";x.lineWidth=1;
 for(let g=0;g<=5;g++){const v=maxY*g/5,y=py(v);x.beginPath();x.moveTo(L,y);x.lineTo(W-R,y);x.stroke();x.textAlign="right";x.fillText(Number.isInteger(v)?v:v.toFixed(1),L-6,y+4)}
 x.textAlign="left";x.fillStyle="#9aa6b5";x.fillText("MiB/s",L+6,T+12);x.fillStyle="#6b7788";x.textAlign="center";x.fillText(fmtSecs(0),L,H-6);x.fillText(fmtSecs(maxX),W-R-18,H-6);
 for(let k=1;k<series.length;k++){const a=series[k-1],b=series[k],col=b.up?"22,163,74":"37,99,235";
  x.beginPath();x.moveTo(px(a.x),py(0));x.lineTo(px(a.x),py(a.y));x.lineTo(px(b.x),py(b.y));x.lineTo(px(b.x),py(0));x.closePath();
  x.fillStyle=`rgba(${col},.18)`;x.fill();x.beginPath();x.moveTo(px(a.x),py(a.y));x.lineTo(px(b.x),py(b.y));
  x.strokeStyle=`rgb(${col})`;x.lineWidth=2;x.stroke()}
 const cur=series[series.length-1].y,avg=series.reduce((s,p)=>s+p.y,0)/series.length,max=Math.max(...series.map(p=>p.y));
 document.getElementById("chartinfo").textContent=`Ahora ${cur.toFixed(1)} MiB/s · media ${avg.toFixed(1)} · máx ${max.toFixed(1)}`}
async function post(p,b){const r=await fetch(p,{method:"POST",body:JSON.stringify(b)});const j=await r.json();if(j.error)throw new Error(j.error);return j}
async function busy(text,fn){const w=document.getElementById("busywrap"),t=document.getElementById("busytxt");
 w.style.display="block";t.textContent=text;const t0=Date.now();
 const tick=setInterval(()=>{t.textContent=text+" ("+Math.round((Date.now()-t0)/1000)+" s)"},1000);
 document.querySelectorAll("button[data-busy]").forEach(b=>b.disabled=true);
 try{return await fn()}finally{clearInterval(tick);w.style.display="none";document.querySelectorAll("button[data-busy]").forEach(b=>b.disabled=false)}}
async function persist(){try{await post("/settings",form())}catch(e){}}
let saved={};
async function listvms(){try{await persist();const j=await busy("Conectando con el origen y listando las VM …",()=>post("/listvms",form()));const s=document.getElementById("vm_select");s.innerHTML='<option value="">(elige una VM)</option>'+j.vms.map(v=>`<option value="${v.name}">${v.name} — ${v.state=="on"?"encendida":"apagada"}, ${v.cpu} vCPU, ${(v.memory_mb/1024).toFixed(1)} GiB${v.host?" · host "+v.host:""}</option>`).join("");show("Conectado a "+j.kind+": "+j.vms.length+" VM(s) encontradas. Elige una en el desplegable.")}catch(e){show("ERROR: "+e.message)}}
let newNameTouched=false;
function syncNewName(){if(!newNameTouched)document.getElementById("new_vm_name").value=document.getElementById("vm").value.trim()}
async function analyze(force){const vm=document.getElementById("vm").value.trim();syncNewName();
 if(!vm||!document.getElementById("source_host").value.trim())return;
 if(!force&&analyzedVm===vm)return; // same VM: keep the disks the user ticked
 try{await persist();const j=await busy("Analizando la VM …",()=>post("/inspect",form()));analyzedVm=vm;renderDisks(j)}
 catch(e){document.getElementById("diskpanel").style.display="none";analyzedVm=null;show("ERROR: "+e.message)}}
let planTouched=false,lastSpec=null;
// Choose the plan that matches the source VM (exact cores+memory, else the smallest one that is big enough).
function pickPlan(){const sel=document.getElementById("plan_id"),hint=document.getElementById("planhint");if(!lastSpec)return;
 const need=lastSpec.mem*1048576,opts=[...sel.options].filter(o=>o.dataset.cores);
 if(!opts.length){hint.textContent="";return}
 let best=opts.find(o=>+o.dataset.cores===lastSpec.cpu&&+o.dataset.mem===need);
 const exact=!!best;
 if(!best)best=opts.filter(o=>+o.dataset.cores>=lastSpec.cpu&&+o.dataset.mem>=need).sort((a,b)=>(+a.dataset.mem-+b.dataset.mem)||(+a.dataset.cores-+b.dataset.cores))[0];
 if(best){sel.value=best.value;hint.textContent=exact?"(coincide con la VM original)":"(el más pequeño que cubre la VM original)"}
 else hint.textContent="(ningún plan cubre "+lastSpec.cpu+" vCPU y "+lastSpec.mem+" MiB: elige uno)"}
function renderDisks(j){lastSpec={cpu:j.cpu,mem:j.memory_mb};if(!planTouched)pickPlan();document.getElementById("diskpanel").style.display="block";
 document.getElementById("diskinfo").innerHTML=`<b>${j.name}</b>: ${j.cpu} vCPU, ${(j.memory_mb/1024).toFixed(1)} GiB RAM, ${j.firmware.toUpperCase()}, ${j.power_state==="on"?"encendida":"apagada"}, ${j.disks.length} disco(s)`;
 document.getElementById("disklist").innerHTML=j.disks.map(d=>`<label style="display:block;color:#1b2430;font-size:14px"><input type="checkbox" class="diskchk" value="${d.index}" checked onchange="disksChanged()"> ${d.name} — ${gib(d.size_bytes)} provisionados${d.used_bytes?` (${gib(d.used_bytes)} usados)`:""}</label>`).join("");
 disksChanged()}
function disksChanged(){const n=document.querySelectorAll(".diskchk:checked").length,t=document.querySelectorAll(".diskchk").length;
 document.getElementById("multi_disk_bundle").checked=n>1;
 document.getElementById("diskhint").textContent=n===0?"Selecciona al menos un disco.":n>1?`${n} discos: se activa "Varios discos en una sola imagen". El primero marcado se trata como disco de arranque.`:`1 disco seleccionado${t>1?" (los demás no se migrarán)":""}.`}
async function speedtest(){try{await persist();
 const j=await busy("Midiendo la velocidad (unos 20 s por cada vía) …",()=>post("/speedtest",form()));
 const line=(n,r)=>r.error?`${n}: no se pudo medir (${r.error})`:`${n}: ${r.mib_s.toFixed(1)} MiB/s (${r.mib.toFixed(0)} MiB leídos)`;
 show(line("Exportación (la que usa el programa)",j.export)+"\n"+line("Disco plano (como el navegador)",j.flat)
 +(j.flat_parallel?"\n"+(j.flat_parallel.error?`Disco plano en paralelo: no se pudo medir (${j.flat_parallel.error})`:`Disco plano con ${j.flat_parallel.streams} conexiones a la vez: ${j.flat_parallel.mib_s.toFixed(1)} MiB/s`+(j.flat&&j.flat.mib_s?` (${(j.flat_parallel.mib_s/j.flat.mib_s).toFixed(1)}× la de una sola conexión)`:"")):"")
 +"\n\nSi la exportación va mucho más lenta que el disco plano, el límite es la compresión del ESXi, no el programa. Si varias conexiones suman mucho más que una, descargar el disco plano en paralelo compensaría.")}
 catch(e){show("ERROR: "+e.message)}}
async function inspect_(){await analyze(true);if(analyzedVm)show("VM analizada: revisa el panel de discos.")}
async function quit(){if(currentJob&&!confirm("Hay una migración en curso. ¿Salir igualmente?"))return;
 try{await post("/shutdown",{})}catch(e){}
 document.body.innerHTML="<main><h1>Programa cerrado</h1><p>Ya puedes cerrar esta pestaña.</p></main>"}
if(EMBEDDED){const q=document.querySelector(".quit");if(q)q.style.display="none"}
setInterval(()=>fetch("/ping").catch(()=>{}),5000);fetch("/ping").catch(()=>{});
async function refinfo(){try{show(JSON.stringify(await busy("Leyendo la instancia de referencia …",()=>post("/refinfo",form())),null,2))}catch(e){show("ERROR: "+e.message)}}
async function lookups(){try{await persist();const j=await busy("Cargando datos de Morpheus …",()=>post("/lookups",form()));
 for(const [k,id] of [["clouds","cloud_id"],["groups","group_id"],["plans","plan_id"],["networks","network_id"],["instances","ref_instance_id"]]){
  const s=document.getElementById(id);s.innerHTML=(id=="ref_instance_id"?'<option value="">(ninguna)</option>':id=="network_id"?'<option value="">(elige una red)</option>':'')+j[k].map(([v,n,c,m])=>`<option value="${v}"${c?` data-cores="${c}" data-mem="${m}"`:""}>${n}</option>`).join("");if(saved[id])s.value=saved[id];else if(id=="network_id"&&j[k].length===1)s.value=j[k][0][0]}
 if(lastSpec&&!planTouched)pickPlan()
  for(const [k,id] of [["layouts","layout_id"],["pools","resource_pool"]]){const s=document.getElementById(id);s.innerHTML='<option value="">(automático)</option>'+j[k].map(([v,n])=>`<option value="${v}">${n}</option>`).join("");if(saved[id])s.value=saved[id]}
 show("Datos cargados.")}catch(e){show("ERROR: "+e.message)}}
const SEP=String.fromCharCode(92);
function srcHint(){const t=document.getElementById("source_type").value;
 document.getElementById("source_user").placeholder={hyperv:"Administrador",vmware:"usuario@vsphere.local",esxi:"root"}[t]||"";
 document.getElementById("os_family").closest("div").style.display=t==="hyperv"?"":"none"}
let pickerState={path:"",parent:null};
const gib=b=>b==null?"?":b<1073741824?Math.max(1,Math.round(b/1048576))+" MiB":(b/1073741824).toFixed(1)+" GiB";
async function openPicker(path){try{const r=await fetch("/browse?path="+encodeURIComponent(path||""));const j=await r.json();if(j.error)throw new Error(j.error);
 pickerState=j;const box=document.getElementById("picker");box.style.display="block";
 document.getElementById("pickerhead").innerHTML=j.path?`<b>${j.path}</b> — ${gib(j.free)} libres`:"<b>Elige un disco</b>";
 const list=document.getElementById("pickerlist");list.innerHTML="";
 const add=(txt,go)=>{const d=document.createElement("div");d.className="item";d.textContent=txt;d.onclick=go;list.appendChild(d)};
 j.drives.forEach(d=>add(`${d.path} — ${gib(d.free)} libres de ${gib(d.total)} (${d.local?"local":"red/otro"})`,()=>openPicker(d.path)));
 j.dirs.forEach(n=>add("📁 "+n,()=>openPicker(j.path.endsWith(SEP)||j.path.endsWith("/")?j.path+n:j.path+(j.path.includes(SEP)?SEP:"/")+n)));
 if(!j.drives.length&&!j.dirs.length)add("(sin subcarpetas)",()=>{})}catch(e){show("ERROR: "+e.message)}}
function pickerUp(){openPicker(pickerState.parent||"")}
function pickerUse(){if(pickerState.path){document.getElementById("workdir").value=pickerState.path;closePicker()}else show("Entra primero en un disco o carpeta.")}
function closePicker(){document.getElementById("picker").style.display="none"}
let currentJob=null;
function drawBar(r){const w=document.getElementById("barwrap"),b=document.getElementById("bar"),l=document.getElementById("barlabel");
 const p=r.progress;if(!p&&r.status==="running"){return}w.style.display="block";
 let pct=Math.round(((p&&p.overall)||0)*100);if(r.status==="done")pct=100;
 b.style.width=pct+"%";b.className=r.status==="done"?"ok":r.status==="error"?"err":r.status==="cancelled"?"stop":"";
 let txt=pct+"% completado (estimado)";
 if(r.status==="running"&&p&&p.step)txt=`Paso ${p.index} de ${p.total_steps}: ${p.label} (${p.fraction==null?"en curso":Math.round(p.fraction*100)+"%"}) · Total estimado ${pct}%`;
 else if(r.status==="done")txt="Terminado · 100%";else if(r.status==="error")txt=`Error · ${pct}% completado`;else if(r.status==="cancelled")txt=`Cancelado · ${pct}% completado`;
 l.textContent=txt}
async function cancelJob(){if(!currentJob)return;document.getElementById("cancelbtn").disabled=true;try{await post("/cancel",{job:currentJob})}catch(e){show("ERROR: "+e.message)}}
async function run(p){try{await persist();const {job}=await post(p,form());currentJob=job;show("Trabajo "+job+" en marcha...");
 const btn=document.getElementById("cancelbtn");btn.style.display="inline-block";btn.disabled=false;resetChart();document.getElementById("barwrap").style.display="none";
 const t=setInterval(async()=>{const r=await (await fetch("/job/"+job)).json();show(r.log||"...");drawBar(r);feedChart(r);if(r.status!=="running"){clearInterval(t);btn.style.display="none";currentJob=null}},1000)}catch(e){show("ERROR: "+e.message)}}
srcHint();
(async()=>{try{saved=await (await fetch("/settings")).json();
 ids.forEach(i=>{if(saved[i]!==undefined&&document.getElementById(i).tagName==="INPUT"||saved[i]!==undefined&&["source_type","os_family","vmware_method"].includes(i))document.getElementById(i).value=saved[i]});
 newNameTouched=!!(saved.new_vm_name&&saved.new_vm_name!==saved.vm); // a name typed earlier is kept
 chks.forEach(i=>{if(saved[i]!==undefined)document.getElementById(i).checked=!!saved[i]});
 srcHint();
 if(saved.morpheus_url&&saved.morpheus_token)lookups()}catch(e){}})();
</script></html>"""
