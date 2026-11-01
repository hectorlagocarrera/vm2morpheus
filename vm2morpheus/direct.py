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
import struct
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from .util import CancelledError

PLACEHOLDER_NAME = "vm2morpheus-vacia"
CHUNK = 4 * 1024 * 1024


class DirectError(RuntimeError):
    pass


# Runs on the host (python3 ships with the Ubuntu of the HVM hosts): reads frames of 8-byte offset + 4-byte length +
# data from stdin and writes them at that offset; a zero-length frame sets the final size and ends.
RECEIVER = (
    "import os,struct,sys\n"
    "r=sys.stdin.buffer\n"
    "while r.readline()!=b'VM2M\\n': pass\n"  # skips a sudo password line that sudo did not need to read
    "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT,0o644)\n"
    "def rd(n):\n"
    " b=b''\n"
    " while len(b)<n:\n"
    "  c=r.read(n-len(b))\n"
    "  if not c: sys.exit('flujo cortado')\n"
    "  b+=c\n"
    " return b\n"
    "while True:\n"
    " off,n=struct.unpack('>QI',rd(12))\n"
    " if n==0:\n"
    "  os.ftruncate(fd,off); break\n"
    " os.pwrite(fd,rd(n),off)\n"
    "os.fsync(fd); os.close(fd); print('ok')\n"
)


SUDO_PW = "sudo -k -S -p '' "  # -S: password on stdin (the SSH password); -k: always asks, so stdin stays in step


def as_user_prefix(ssh_user: str, owner: str | None, password: bool = False) -> str:
    """Command prefix to act as `owner` (the owner of the datastore folder) instead of the SSH user."""
    if not owner or owner == ssh_user:
        return ""
    q = shlex.quote(owner)
    if ssh_user == "root":
        return f"runuser -u {q} -- "
    return f"{SUDO_PW}-u {q} -- " if password else f"sudo -n -u {q} -- "


def _frames(src, start: int, total: int | None, progress=None, cancel=None, size_hint: int | None = None):
    """(offset, data) chunks of a file/stream from `start`, zero chunks left out, then (final size, b"")."""
    zero = bytes(CHUNK)
    off = start
    while True:
        if cancel is not None and cancel.is_set():
            raise CancelledError("cancelado")
        buf = src.read(CHUNK)
        if not buf:
            break
        if buf != zero[:len(buf)]:
            yield off, buf
        off += len(buf)
        if progress:
            progress(off, total or size_hint)
    yield (total if total is not None else off), b""


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
        self.user, self._password = user, password

    def run(self, cmd: str, check: bool = True, timeout: float = 3600) -> str:
        stdin, out, err = self.client.exec_command(cmd, timeout=timeout)
        if SUDO_PW in cmd and self._password:  # one password line per sudo in the command
            stdin.write((self._password + "\n") * cmd.count(SUDO_PW))
            stdin.flush()
        stdin.channel.shutdown_write()
        text, etext = out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")
        code = out.channel.recv_exit_status()
        if check and code != 0 and ("a password is required" in etext or "incorrect password" in etext
                                    or "Sorry, try again" in etext or "no tty present" in etext):
            raise DirectError(f"En {self.host}, el usuario {self.user} necesita sudo y no se pudo usar con la "
                              "contraseña indicada: usa root o un usuario con sudo y su contraseña")
        if check and code != 0:
            raise DirectError(f"En {self.host} falló «{cmd}» ({code}): {(etext or text).strip()[-500:]}")
        return text

    def as_user(self, owner: str | None) -> str:
        return as_user_prefix(self.user, owner, bool(getattr(self, "_password", None)))

    def remote_size(self, path: str, owner: str | None = None) -> int | None:
        out = self.run(f"{self.as_user(owner)}stat -c %s {shlex.quote(path)}", check=False).strip()
        return int(out) if out.isdigit() else None

    def send_frames(self, remote: str, frames, owner: str | None = None) -> None:
        """Write (offset, bytes) frames into `remote` through a tiny receiver run as `owner` on the host.

        Not SFTP: on an NFS datastore exported with root_squash, root may not write into the VM folders, which
        belong to libvirt-qemu; the receiver runs as the folder's owner instead. The last frame (offset, b"")
        sets the final size, so skipped zero runs stay as holes."""
        cmd = f"{self.as_user(owner)}python3 -c {shlex.quote(RECEIVER)} {shlex.quote(remote)}"
        chan = self.client.get_transport().open_session()
        chan.exec_command(cmd)
        try:
            if SUDO_PW in cmd:
                chan.sendall((self._password + "\n").encode())
            chan.sendall(b"VM2M\n")
            for off, data in frames:
                chan.sendall(struct.pack(">QI", off, len(data)) + data)
                if not data:
                    break
            chan.shutdown_write()
        except OSError:
            pass  # the receiver died: its message is read below
        out = chan.makefile("rb").read().decode("utf-8", "replace")
        err = chan.makefile_stderr("rb").read().decode("utf-8", "replace")
        code = chan.recv_exit_status()
        if "a password is required" in err or "incorrect password" in err.lower() or "contraseña" in err.lower():
            raise DirectError(f"En {self.host}, sudo no acepta la contraseña del usuario {self.user}: usa root o un "
                              "usuario con sudo y su contraseña")
        if code != 0 or "ok" not in out:
            raise DirectError(f"En {self.host} no se pudo escribir {remote}" + (f" como {owner}" if owner else "")
                              + f" ({code}): {(err or out).strip()[-400:]}")

    def upload(self, local: Path, remote: str, progress=None, cancel=None, owner: str | None = None) -> None:
        """Copy local -> remote, resuming after what a previous attempt already wrote."""
        total = local.stat().st_size
        start = self.remote_size(remote, owner) or 0
        if start > total:
            start = 0
        with open(local, "rb") as src:
            src.seek(start)
            self.send_frames(remote, _frames(src, start, total, progress, cancel), owner)

    def write_stream(self, reader, remote: str, size: int, progress=None, cancel=None, owner: str | None = None) -> None:
        """Write a stream to a remote file. Runs of zeros are not sent (left as holes), so an empty or thin disk
        costs reading but not network to the host nor datastore writes."""
        done = {"n": 0}

        def frames():
            for off, data in _frames(reader, 0, None, progress, cancel, size):
                done["n"] = off if not data else off + len(data)
                yield off, data

        self.send_frames(remote, frames(), owner)
        if size and done["n"] < size:
            raise DirectError(f"El origen dejó de enviar el disco a los {done['n']} de {size} bytes")

    def close(self):
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
    owner = None
    for who in (shell.run(f"stat -c %U {q(posixpath.dirname(posixpath.dirname(staging)))}", check=False).strip() or None,
                "libvirt-qemu"):
        if shell.run(f"{shell.as_user(who)}mkdir -p {q(staging)} && echo ok", check=False).strip() == "ok":
            owner = who
            break
    else:
        raise DirectError(f"No se pudo crear {staging} en el almacén de datos (permisos)")
    pre = shell.as_user(owner)
    if shell.run(f"test -f {q(out)} && echo yes", check=False).strip() == "yes":
        log(f"{name}: ya estaba copiado y convertido en el host, se reutiliza")
        return out
    try:
        shell.write_stream(reader, src, size, progress, cancel, owner)
    finally:
        reader.close()
    log(f"Convirtiendo {name} a qcow2 en el host ...")
    shell.run(f"{pre}qemu-img convert -f {q(fmt)} -O qcow2 {q(src)} {q(out + '.part')} && {pre}mv -f {q(out + '.part')} "
              f"{q(out)} && {pre}rm -f {q(src)}")
    return out


def replace_disk(shell, local, target: str, log=print, progress=None, cancel=None) -> None:
    """Swap the disk file `target` on the host for `local`: a local qcow2 that is uploaded first, or (str) the
    path of a qcow2 already prepared on the same datastore by stage_disk. Everything is done as the owner of the
    VM folder (libvirt-qemu), which also works on NFS exports with root_squash."""
    q = shlex.quote
    owner = shell.run(f"stat -c %U {q(posixpath.dirname(target))}", check=False).strip() or None
    pre = shell.as_user(owner)
    info = json.loads(shell.run(f"{pre}qemu-img info --output=json {q(target)}"))
    want = int(info["virtual-size"])
    if isinstance(local, str):  # already on the host
        part, label = local, posixpath.basename(local)
    else:
        part, label = posixpath.join(posixpath.dirname(target), "." + posixpath.basename(target) + ".vm2m-part"), local.name
        shell.upload(local, part, progress, cancel, owner)
    got = int(json.loads(shell.run(f"{pre}qemu-img info --output=json {q(part)}"))["virtual-size"])
    if got > want:
        raise DirectError(f"El disco migrado ({got} bytes) es mayor que el volumen que creó Morpheus ({want} bytes)")
    if got < want:
        shell.run(f"{pre}qemu-img resize -f qcow2 {q(part)} {want}")
    shell.run(f"{pre}qemu-img check -q {q(part)}")
    shell.run(f"{pre}chmod --reference={q(target)} {q(part)}")
    if not pre:  # acting as root on a local folder: give the file the owner libvirt expects
        shell.run(f"chown --reference={q(target)} {q(part)}", check=False)
    shell.run(f"{pre}mv -f {q(part)} {q(target)}")
    if isinstance(local, str):  # the staging folder is empty once its last disk has moved
        shell.run(f"{pre}rmdir {q(posixpath.dirname(local))}", check=False)
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


def _is_off(shell, vm_name: str) -> bool:
    return domstate(shell, vm_name) in ("shut off", "apagado")


def power_off(morpheus, instance_id: int, shell, vm_name: str, log=print, sleep=time.sleep) -> None:
    """Stop the VM through Morpheus (so it knows the VM is meant to be off: clusters with "auto recover power state"
    switch on any VM that is powered off behind their back), then force it off with virsh destroy after a few
    seconds: the VM only has the blank placeholder disk, so there is nothing to shut down cleanly."""
    try:
        morpheus.instance_action(instance_id, "stop")  # mainly so Morpheus knows it is meant to be off
        for _ in range(5):  # Morpheus asks for a clean (ACPI) shutdown, which a blank disk never answers
            if _is_off(shell, vm_name):
                return
            sleep(2)
    except Exception as e:  # e.g. Morpheus refuses actions while the instance is still provisioning
        log(f"Morpheus no pudo apagarla ({e}); se apaga en el host")
    shell.run(f"{VIRSH} destroy {shlex.quote(vm_name)}", check=False)
    for _ in range(30):
        if _is_off(shell, vm_name):
            return
        sleep(2)
    raise DirectError(f"La VM {vm_name!r} no se apagó: no se tocan sus discos")


def stale_disks(shell, vm_name: str, targets: list[str]) -> list[str]:
    """Disk paths the VM's running qemu process still holds as deleted files, i.e. it was started before they were
    replaced. Empty when it is not running on this host or the check is not possible."""
    script = (f"pid=$(cat /run/libvirt/qemu/{shlex.quote(vm_name)}.pid 2>/dev/null) && "
              "[ -n \"$pid\" ] && ls -l /proc/$pid/fd 2>/dev/null")
    out = shell.run(f"{shell.as_user('root')}sh -c {shlex.quote(script)}", check=False)
    held = [line for line in out.splitlines() if "(deleted)" in line]
    return [t for t in targets if any(f"-> {t} (deleted)" in line for line in held)]


def power_on(morpheus, instance_id: int, shell, vm_name: str, log=print, sleep=time.sleep) -> None:
    try:
        morpheus.instance_action(instance_id, "start")
        for _ in range(30):
            if not _is_off(shell, vm_name):
                return
            sleep(2)
    except Exception as e:
        log(f"Morpheus no pudo arrancarla ({e}); se arranca en el host")
    if _is_off(shell, vm_name):
        shell.run(f"{VIRSH} start {shlex.quote(vm_name)}")


def push_disks(morpheus, instance_id: int, vm_name: str, disks: list, ssh: dict, log=print,
               progress_for=None, cancel=None, connect=HostShell, sleep=time.sleep) -> str:
    """Power off the new VM, replace its disk files with `disks` (boot disk first) and start it again.

    The VM is found with virsh on whichever cluster host Morpheus placed it. Power actions go through Morpheus
    when it accepts them and through virsh otherwise. Each disk file is swapped in one step (mv), so even if the
    VM is switched on meanwhile (auto recover) it keeps the old file open and the new one is intact; that is why
    it is always fully stopped and started again at the end, so the new process opens the new disks."""
    log("Esperando a que la VM aparezca en el host (Morpheus la está creando) ...")
    log("Hosts donde se buscará: " + ", ".join(candidate_hosts(morpheus, instance_id, ssh.get("host", ""))))
    shell, host, vm_name, xml = find_domain(morpheus, instance_id, vm_name, ssh, log, connect, sleep=sleep)
    log(f"VM encontrada en {host} (en libvirt se llama {vm_name!r})")
    try:
        if not _is_off(shell, vm_name):
            log(f"Apagando la VM recién creada (solo tiene el disco vacío de la plantilla) ...")
            power_off(morpheus, instance_id, shell, vm_name, log, sleep)
        xml = shell.run(f"{VIRSH} dumpxml {shlex.quote(vm_name)}")  # again: it may have changed while booting
        targets = domain_disks(xml)
        if len(targets) < len(disks):
            raise DirectError(f"La VM tiene {len(targets)} disco(s) en {host} y hay {len(disks)} para subir")
        log(f"Subida directa por SSH a {host}: {len(disks)} disco(s)")
        for i, (local, target) in enumerate(zip(disks, targets)):
            pf = progress_for(i) if progress_for and not isinstance(local, str) else None
            replace_disk(shell, local, target, log, pf, cancel)
        if not _is_off(shell, vm_name):  # switched on meanwhile: it still runs on the old (blank) files
            log("La VM se encendió durante la copia (Morpheus recupera el estado de encendido); se apaga otra vez "
                "para que arranque con los discos nuevos ...")
            power_off(morpheus, instance_id, shell, vm_name, log, sleep)
        log("Arrancando la VM con los discos migrados ...")
        power_on(morpheus, instance_id, shell, vm_name, log, sleep)
        for attempt in range(2):  # make sure the running process opened the NEW files, not the replaced ones
            stale = stale_disks(shell, vm_name, targets[:len(disks)])
            if not stale:
                break
            log("La VM sigue usando el disco vacío anterior (" + ", ".join(posixpath.basename(t) for t in stale)
                + "); se apaga y se enciende de nuevo ...")
            shell.run(f"{VIRSH} destroy {shlex.quote(vm_name)}", check=False)
            for _ in range(30):
                if _is_off(shell, vm_name):
                    break
                sleep(2)
            power_on(morpheus, instance_id, shell, vm_name, log, sleep)
        else:
            if stale_disks(shell, vm_name, targets[:len(disks)]):
                raise DirectError("Los discos se copiaron, pero la VM sigue arrancada con el disco vacío anterior. "
                                  "Apágala y enciéndela desde Morpheus (Stop y Start, no Restart).")
    finally:
        shell.close()
    log("VM arrancada. Si Morpheus la sigue mostrando como 'provisioning', la dará por creada cuando Windows arranque "
        "y obtenga una dirección de red.")
    return host
