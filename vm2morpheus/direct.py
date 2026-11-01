"""Direct disk upload over SSH to the HVM host (Morpheus VM Essentials), skipping the image library.

Morpheus only accepts whole images through its API, and on the first provision it copies the library image again
to the cluster datastore. Here the VM is created from a tiny blank placeholder image, stopped, and its disk files
on the host are replaced with the migrated disks, so every disk travels exactly once.

The disk files are found with `virsh dumpxml` on the host that runs the VM. Each new disk is uploaded next to the
original as a hidden part file (resumable), grown to the size Morpheus gave the volume, checked with qemu-img,
given the original's owner and permissions and only then moved over the original in one step.
"""
import json
import posixpath
import shlex
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .util import CancelledError

PLACEHOLDER_NAME = "vm2morpheus-vacia"
CHUNK = 4 * 1024 * 1024


class DirectError(RuntimeError):
    pass


class HostShell:
    """SSH session to one HVM host (paramiko): commands and SFTP."""

    def __init__(self, host: str, user: str, password: str | None = None, key_file: str | None = None,
                 port: int = 22, timeout: float = 20):
        import paramiko  # imported here: only needed when the direct upload is used
        self.host = host
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # lab hosts: no known_hosts on Windows
        try:
            self.client.connect(host, port=port, username=user, password=password or None,
                                key_filename=key_file or None, timeout=timeout, banner_timeout=timeout,
                                auth_timeout=timeout, look_for_keys=not password, allow_agent=not password)
        except paramiko.AuthenticationException as e:
            raise DirectError(f"SSH a {host}: usuario o contraseña/clave incorrectos ({e})") from e
        except (OSError, paramiko.SSHException) as e:
            raise DirectError(f"No se pudo conectar por SSH a {host}: {e}") from e
        self._sftp = None

    def run(self, cmd: str, check: bool = True, timeout: float = 3600) -> str:
        _, out, err = self.client.exec_command(cmd, timeout=timeout)
        text, etext = out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        if check and code != 0:
            raise DirectError(f"En {self.host} falló «{cmd}» ({code}): {(etext or text).strip()[-500:]}")
        return text

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def remote_size(self, path: str) -> int | None:
        try:
            return self.sftp.stat(path).st_size
        except OSError:
            return None

    def upload(self, local: Path, remote: str, progress=None, cancel=None) -> None:
        """Copy local -> remote, resuming after what a previous attempt already wrote."""
        total = local.stat().st_size
        start = self.remote_size(remote) or 0
        if start > total:
            start = 0
        with open(local, "rb") as src, self.sftp.open(remote, "r+b" if start else "wb") as dst:
            dst.set_pipelined(True)
            src.seek(start)
            dst.seek(start)
            done = start
            while True:
                if cancel is not None and cancel.is_set():
                    raise CancelledError("cancelado")
                buf = src.read(CHUNK)
                if not buf:
                    break
                dst.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total)

    def write_stream(self, reader, remote: str, size: int, progress=None, cancel=None) -> None:
        """Write a stream to a remote file. Runs of zeros are skipped (left as holes), so an empty or thin disk
        costs network but not datastore writes; the file is then truncated to its full size."""
        zero = bytes(CHUNK)
        done = 0
        with self.sftp.open(remote, "wb") as dst:
            dst.set_pipelined(True)
            while True:
                if cancel is not None and cancel.is_set():
                    raise CancelledError("cancelado")
                buf = reader.read(CHUNK)
                if not buf:
                    break
                if buf == zero[:len(buf)]:
                    dst.seek(done + len(buf))
                else:
                    dst.seek(done)
                    dst.write(buf)
                done += len(buf)
                if progress:
                    progress(done, size)
            dst.truncate(max(done, 0))
        if size and done < size:
            raise DirectError(f"El origen dejó de enviar el disco a los {done} de {size} bytes")

    def close(self):
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self.client.close()


def domain_disks(xml_text: str) -> list[str]:
    """Paths of the VM's disk files (no CD-ROMs): the boot disk first, then the others in the order defined."""
    root = ET.fromstring(xml_text)
    disks = []
    for i, d in enumerate(root.findall("./devices/disk")):
        if d.get("device") != "disk":
            continue
        src = d.find("source")
        path = src.get("file") if src is not None else None
        if not path:
            raise DirectError("Un disco de la VM no es un fichero (almacenamiento de bloques): la subida directa solo "
                              "funciona con almacenes de datos de ficheros (NFS, GFS2, directorio local)")
        boot = d.find("boot")
        order = int(boot.get("order")) if boot is not None and boot.get("order") else 10_000
        disks.append((order, i, path))
    return [p for _, _, p in sorted(disks)]


def candidate_hosts(morpheus, instance_id: int, given: str) -> list[str]:
    """Where the VM may be defined: the host Morpheus says runs it, then the host typed by the user, then every
    HVM host it knows. Lookups that fail are skipped."""
    found: list[str] = []

    def add(h):
        if h and h not in found:
            found.append(h)

    def add_server(server: dict) -> None:  # every address Morpheus knows: the first reachable one wins
        for k in ("sshHost", "internalIp", "externalIp", "hostname", "name"):
            add(server.get(k))

    try:
        inst = morpheus.get_instance(instance_id)
        for sid in inst.get("servers") or []:
            vm = morpheus._req("GET", f"/api/servers/{sid}").get("server", {})
            parent = (vm.get("parentServer") or {}).get("id")
            if parent:
                add_server(morpheus._req("GET", f"/api/servers/{parent}").get("server", {}))
    except Exception:
        pass
    for h in (given or "").replace(";", ",").split(","):  # the user may list several hosts
        add(h.strip())
    try:
        for s in morpheus._req("GET", "/api/servers", params={"max": 200}).get("servers", []):
            if (s.get("computeServerType") or {}).get("code") == "mvmHost":
                add_server(s)
    except Exception:
        pass
    return found


def wait_instance(morpheus, instance_id: int, wanted: set[str], timeout: float = 1800, poll: float = 5,
                  sleep=time.sleep, clock=time.monotonic) -> str:
    deadline = clock() + timeout
    while True:
        status = (morpheus.get_instance(instance_id).get("status") or "").lower()
        if status in wanted:
            return status
        if status == "failed":
            raise DirectError("Morpheus marcó la instancia como fallida al crearla; revisa su historial en Morpheus")
        if clock() > deadline:
            raise DirectError(f"La instancia sigue en estado {status!r} tras {timeout / 60:.0f} min")
        sleep(poll)


def stage_disk(shell, reader, fmt: str, size: int, staging: str, name: str, log=print, progress=None,
               cancel=None) -> str:
    """Send one data disk straight from the source to `staging` (a folder on the datastore) and convert it to
    qcow2 there with the host's qemu-img. Returns the remote qcow2 path. A disk already converted is reused."""
    q = shlex.quote
    out = posixpath.join(staging, name + ".qcow2")
    src = posixpath.join(staging, name + "." + fmt + ".src")
    shell.run(f"mkdir -p {q(staging)}")
    if shell.run(f"test -f {q(out)} && echo yes", check=False).strip() == "yes":
        log(f"{name}: ya estaba copiado y convertido en el host, se reutiliza")
        return out
    try:
        shell.write_stream(reader, src, size, progress, cancel)
    finally:
        reader.close()
    log(f"Convirtiendo {name} a qcow2 en el host ...")
    shell.run(f"qemu-img convert -f {q(fmt)} -O qcow2 {q(src)} {q(out + '.part')} && mv -f {q(out + '.part')} {q(out)} "
              f"&& rm -f {q(src)}")
    return out


def replace_disk(shell, local, target: str, log=print, progress=None, cancel=None) -> None:
    """Swap the disk file `target` on the host for `local`: a local qcow2 that is uploaded first, or (str) the
    path of a qcow2 already prepared on the same datastore by stage_disk."""
    q = shlex.quote
    info = json.loads(shell.run(f"qemu-img info --output=json {q(target)}"))
    want = int(info["virtual-size"])
    if isinstance(local, str):  # already on the host
        part, label = local, posixpath.basename(local)
    else:
        part, label = posixpath.join(posixpath.dirname(target), "." + posixpath.basename(target) + ".vm2m-part"), local.name
        shell.upload(local, part, progress, cancel)
    got = int(json.loads(shell.run(f"qemu-img info --output=json {q(part)}"))["virtual-size"])
    if got > want:
        raise DirectError(f"El disco migrado ({got} bytes) es mayor que el volumen que creó Morpheus ({want} bytes)")
    if got < want:
        shell.run(f"qemu-img resize -f qcow2 {q(part)} {want}")
    shell.run(f"qemu-img check -q {q(part)}")
    shell.run(f"chown --reference={q(target)} {q(part)} && chmod --reference={q(target)} {q(part)}")
    shell.run(f"mv -f {q(part)} {q(target)}")
    if isinstance(local, str):  # the staging folder is empty once its last disk has moved
        shell.run(f"rmdir {q(posixpath.dirname(local))} 2>/dev/null", check=False)
    log(f"Disco {posixpath.basename(target)} sustituido por {label}")


VIRSH = "virsh -c qemu:///system"  # explicit: a non-root SSH user would otherwise see only its own (empty) session


def match_domain(names: list[str], vm_name: str) -> str | None:
    """The libvirt domain of the VM among `names`: the exact name, else a single case-insensitive match."""
    if vm_name in names:
        return vm_name
    close = [n for n in names if n.lower() == vm_name.lower()]
    return close[0] if len(close) == 1 else None


def find_domain(morpheus, instance_id: int, vm_name: str, ssh: dict, log=print, connect=None, timeout: float = 1800,
                poll: float = 10, sleep=time.sleep, clock=time.monotonic, report_every: float = 60):
    """(shell, host, domain name, xml) of the host where the new VM's libvirt domain is defined, waiting until
    Morpheus has defined it. Says every minute which hosts were tried and what each answered."""
    connect = connect or HostShell
    shells, status, failed = {}, {}, {}
    start = clock()
    deadline, next_report = start + timeout, start + report_every
    try:
        while True:
            hosts = candidate_hosts(morpheus, instance_id, ssh.get("host", ""))
            for host in hosts:
                if host not in shells:
                    if clock() - failed.get(host, -1e9) < 120:  # unreachable a moment ago: do not wait 20 s again
                        continue
                    try:
                        shells[host] = connect(host, ssh.get("user") or "root", ssh.get("password"), ssh.get("key_file"))
                    except DirectError as e:
                        status[host], failed[host] = str(e), clock()
                        continue
                out = shells[host].run(f"{VIRSH} list --all --name", check=False)
                names = [n.strip() for n in out.splitlines() if n.strip()]
                name = match_domain(names, vm_name)
                if name:
                    xml = shells[host].run(f"{VIRSH} dumpxml {shlex.quote(name)}", check=False)
                    if "<domain" in xml:
                        return shells.pop(host), host, name, xml
                status[host] = (f"{host}: conectado, pero {vm_name!r} no está entre sus VMs "
                                f"({', '.join(names[:8]) or 'ninguna'}{'…' if len(names) > 8 else ''})")
            if not shells and len(failed) >= len(hosts):
                raise DirectError("No se pudo conectar por SSH a ningún host: " + " · ".join(status.values()))
            now = clock()
            if now > deadline:
                raise DirectError(f"La VM no apareció en ningún host en {timeout / 60:.0f} min: "
                                  + " · ".join(status.values()))
            if now >= next_report:
                log(f"Todavía buscando la VM ({(now - start) / 60:.0f} min). Hosts probados: "
                    + " · ".join(status.get(h, h) for h in hosts))
                next_report = now + report_every
            sleep(poll)
    finally:
        for sh in shells.values():
            sh.close()


def domstate(shell, vm_name: str) -> str:
    return shell.run(f"{VIRSH} domstate {shlex.quote(vm_name)}", check=False).strip().lower()


def push_disks(morpheus, instance_id: int, vm_name: str, disks: list, ssh: dict, log=print,
               progress_for=None, cancel=None, connect=HostShell, sleep=time.sleep) -> str:
    """Power off the new VM on its host, replace its disk files with `disks` (boot disk first) and start it.

    Everything happens with virsh on the host: Morpheus keeps the instance in "provisioning" until an operating
    system boots and gets a network address, which never happens with the blank placeholder disk, so its API
    cannot be used to stop/start it at this point. Returns the host used."""
    log("Esperando a que la VM aparezca en el host (Morpheus la está creando) ...")
    log("Hosts donde se buscará: " + ", ".join(candidate_hosts(morpheus, instance_id, ssh.get("host", ""))))
    shell, host, vm_name, xml = find_domain(morpheus, instance_id, vm_name, ssh, log, connect, sleep=sleep)
    log(f"VM encontrada en {host} (en libvirt se llama {vm_name!r})")
    q = shlex.quote(vm_name)
    try:
        if domstate(shell, vm_name) not in ("shut off", "apagado"):
            log(f"Apagando en {host} la VM recién creada (solo tiene el disco vacío de la plantilla) ...")
            shell.run(f"{VIRSH} destroy {q}", check=False)
            for _ in range(30):
                if domstate(shell, vm_name) in ("shut off", "apagado"):
                    break
                sleep(2)
            else:
                raise DirectError(f"La VM {vm_name!r} no se apagó en {host}: no se tocan sus discos")
        xml = shell.run(f"{VIRSH} dumpxml {q}")  # again: the definition may have changed while it was booting
        targets = domain_disks(xml)
        if len(targets) < len(disks):
            raise DirectError(f"La VM tiene {len(targets)} disco(s) en {host} y hay {len(disks)} para subir")
        log(f"Subida directa por SSH a {host}: {len(disks)} disco(s)")
        for i, (local, target) in enumerate(zip(disks, targets)):
            pf = progress_for(i) if progress_for and not isinstance(local, str) else None
            replace_disk(shell, local, target, log, pf, cancel)
        if domstate(shell, vm_name) not in ("shut off", "apagado"):
            raise DirectError(f"Alguien encendió la VM {vm_name!r} mientras se sustituían los discos; apágala, "
                              "reintenta la migración y no la arranques a mano hasta que termine")
        log("Arrancando la VM ...")
        shell.run(f"{VIRSH} start {q}")
    finally:
        shell.close()
    log("VM arrancada. Morpheus la dará por creada cuando Windows arranque y obtenga una dirección de red "
        "(puede tardar unos minutos; el estado 'provisioning' hasta entonces es normal).")
    return host
