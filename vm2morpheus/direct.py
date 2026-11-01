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

    def addr(server: dict) -> str | None:
        return server.get("sshHost") or server.get("internalIp") or server.get("externalIp") or server.get("name")

    try:
        inst = morpheus.get_instance(instance_id)
        for sid in inst.get("servers") or []:
            vm = morpheus._req("GET", f"/api/servers/{sid}").get("server", {})
            parent = (vm.get("parentServer") or {}).get("id")
            if parent:
                add(addr(morpheus._req("GET", f"/api/servers/{parent}").get("server", {})))
    except Exception:
        pass
    add(given)
    try:
        for s in morpheus._req("GET", "/api/servers", params={"max": 200}).get("servers", []):
            if (s.get("computeServerType") or {}).get("code") == "mvmHost":
                add(addr(s))
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


def replace_disk(shell, local: Path, target: str, log=print, progress=None, cancel=None) -> None:
    """Upload `local` (qcow2) and swap it in for the disk file `target` on the host."""
    q = shlex.quote
    part = posixpath.join(posixpath.dirname(target), "." + posixpath.basename(target) + ".vm2m-part")
    info = json.loads(shell.run(f"qemu-img info --output=json {q(target)}"))
    want = int(info["virtual-size"])
    shell.upload(local, part, progress, cancel)
    got = int(json.loads(shell.run(f"qemu-img info --output=json {q(part)}"))["virtual-size"])
    if got > want:
        raise DirectError(f"El disco migrado ({got} bytes) es mayor que el volumen que creó Morpheus ({want} bytes)")
    if got < want:
        shell.run(f"qemu-img resize -f qcow2 {q(part)} {want}")
    shell.run(f"qemu-img check -q {q(part)}")
    shell.run(f"chown --reference={q(target)} {q(part)} && chmod --reference={q(target)} {q(part)}")
    shell.run(f"mv -f {q(part)} {q(target)}")
    log(f"Disco {posixpath.basename(target)} sustituido por {local.name}")


def push_disks(morpheus, instance_id: int, vm_name: str, disks: list[Path], ssh: dict, log=print,
               progress_for=None, cancel=None, connect=HostShell) -> str:
    """Stop the new VM, replace its disk files with `disks` (boot disk first) and start it again.
    Returns the host used."""
    log("Esperando a que Morpheus termine de crear la VM ...")
    status = wait_instance(morpheus, instance_id, {"running", "stopped", "suspended", "warning", "unknown"})
    if status != "stopped":
        log("Apagando la VM recién creada (todavía tiene el disco vacío de la plantilla) ...")
        morpheus.instance_action(instance_id, "stop")
        wait_instance(morpheus, instance_id, {"stopped"}, timeout=600)
    errors = []
    for host in candidate_hosts(morpheus, instance_id, ssh.get("host", "")):
        try:
            shell = connect(host, ssh.get("user") or "root", ssh.get("password"), ssh.get("key_file"))
        except DirectError as e:
            errors.append(str(e))
            continue
        try:
            xml = shell.run(f"virsh dumpxml {shlex.quote(vm_name)}", check=False)
            if "<domain" not in xml:
                errors.append(f"{host}: la VM {vm_name!r} no está definida en este host")
                continue
            state = shell.run(f"virsh domstate {shlex.quote(vm_name)}", check=False).strip().lower()
            if state and state not in ("shut off", "apagado"):
                raise DirectError(f"La VM {vm_name!r} sigue encendida en {host} ({state}): no se tocan sus discos")
            targets = domain_disks(xml)
            if len(targets) < len(disks):
                raise DirectError(f"La VM tiene {len(targets)} disco(s) en {host} y hay {len(disks)} para subir")
            log(f"Subida directa por SSH a {host}: {len(disks)} disco(s)")
            for i, (local, target) in enumerate(zip(disks, targets)):
                replace_disk(shell, local, target, log, progress_for(i) if progress_for else None, cancel)
            break
        finally:
            shell.close()
    else:
        raise DirectError("No se encontró la VM en ningún host por SSH: " + " · ".join(errors))
    log("Arrancando la VM ...")
    morpheus.instance_action(instance_id, "start")
    return host
