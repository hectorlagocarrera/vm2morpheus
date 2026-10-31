import itertools
import json
import os
import shutil
from pathlib import Path

from .bundle import build_bundle, disk_file_name
from .convert import convert_disk, inject_drivers
from .drivers_windows import ensure_virtio_iso, inject_virtio_windows, is_admin
from .morpheus import MorpheusClient, MorpheusError, payload_from_reference, render_template
from .sources.base import Source
from .util import CancelledError, Progress, human, redact

def plan_shortfall(plan: dict | None, cpu: int, memory_mb: int) -> str | None:
    """Warning text if the service plan gives fewer cores or less memory than the source VM has, else None."""
    if not plan:
        return None
    cores, mem = plan.get("maxCores"), plan.get("maxMemory")
    if not cores or not mem or (cores >= cpu and mem >= memory_mb * 2**20):
        return None
    return (f"Aviso: el plan {plan.get('name', '?')!r} da {cores} CPU y {human(mem)} de memoria, y la VM original "
            f"tiene {cpu} vCPU y {memory_mb} MiB. Elige otro plan en el bloque 3 si quieres los mismos recursos.")


def instance_exists_message(name: str, found: dict | None = None) -> str:
    where = f" (id {found['id']}, estado {found.get('status', '?')})" if found and found.get("id") else ""
    return (f"Ya existe en Morpheus una instancia llamada {name!r}{where}. Si es una prueba anterior, bórrala en "
            "Provisioning > Instances y vuelve a pulsar Migrar: el programa conserva lo ya descargado, convertido y "
            "subido y solo creará la VM. Si ya es la VM migrada, no hace falta hacer nada más.")


def volume_related_rejection(e: Exception) -> bool:
    """A 400 that is about the disk/controller we asked for (worth retrying without SATA), not about the name."""
    text = str(e).lower()
    return " 400:" in text and "must be unique" not in text and any(k in text for k in ("controller", "volume", "storage", "disk"))


def create_instance_clearly(morpheus, payload: dict, name: str) -> dict:
    try:
        return morpheus.create_instance(payload)
    except MorpheusError as e:
        if "must be unique" in str(e).lower():
            raise RuntimeError(instance_exists_message(name)) from e
        raise


def _is_windows() -> bool:
    return os.name == "nt"


from .pipeline_steps import STEP_LABELS, STEPS
from . import timings
from .tracker import Tracker


def relevant_steps(opts: dict, mcfg: dict, injected: bool = False) -> list[str]:
    """Steps that apply to this run, so the messages do not mention things that were never requested."""
    steps = list(STEPS)
    if not (opts.get("inject_drivers") or injected):
        steps.remove("drivers")
    if not (mcfg.get("reference_instance") or mcfg.get("instance")):
        steps.remove("instance")
    return steps


def labels(steps: list[str]) -> str:
    return ", ".join(STEP_LABELS[x] for x in steps) or "nada"


class State:
    """Tiny JSON checkpoint so a failed run resumes instead of re-downloading hundreds of GB."""

    def __init__(self, path: Path):
        self.path = path
        self.d = json.loads(path.read_text()) if path.exists() else {}

    def done(self, step):
        return step in self.d.get("done", [])

    def set(self, **data):
        self.d.update(data)
        self.path.write_text(json.dumps(self.d, indent=2))

    def mark(self, step, **data):
        self.d.setdefault("done", []).append(step)
        self.d.update(data)
        self.path.write_text(json.dumps(self.d, indent=2))


def migrate(source: Source, morpheus: MorpheusClient, vm_name: str, opts: dict, mcfg: dict,
            dry_run=False, log=print, cancel=None, on_progress=None) -> dict:
    def check():
        if cancel is not None and cancel.is_set():
            raise CancelledError("cancelled")

    vm = source.get_vm(vm_name)
    for i, d in enumerate(vm.disks):
        if d.index is None:
            d.index = i
    selected = opts.get("disk_indexes")
    if selected is not None:
        total_disks = len(vm.disks)
        vm.disks = [d for d in vm.disks if d.index in set(selected)]
        if not vm.disks:
            raise RuntimeError("No hay ningún disco seleccionado para convertir")
        log(f"Discos seleccionados: {len(vm.disks)} de {total_disks} "
            f"({', '.join(d.name for d in vm.disks)}); el primero se trata como disco de arranque")
    fmt = opts.get("output_format", "qcow2")
    windows_inject = bool(opts.get("inject_drivers")) and _is_windows() and vm.os_family == "windows"
    if windows_inject and source.direct:
        source.direct = False  # drivers are written into the disk, so it must be a private copy, never the original
        log("La inyección de drivers necesita una copia local del disco: se desactiva la lectura directa")
    total = sum(d.size_bytes for d in vm.disks)
    log(f"VM {vm.name}: {vm.cpu} vCPU, {vm.memory_mb} MiB, {vm.firmware.upper()}, "
        f"{vm.os_family}, {'encendida' if vm.power_state == 'on' else 'apagada'}, {len(vm.disks)} disco(s), "
        f"{human(total)} provisionados")

    problems = []
    if vm.has_snapshots:
        problems.append("La VM tiene snapshots o checkpoints: consolídalos primero")
    if not vm.disks:
        problems.append("La VM no tiene discos")
    if windows_inject and not is_admin():
        problems.append("La inyección de drivers necesita permisos de administrador: cierra el programa y ábrelo con "
                        "clic derecho > Ejecutar como administrador (o desmarca la opción de drivers)")
    if vm.power_state == "on" and not opts.get("shutdown_source"):
        problems.append("La VM está encendida: marca 'Apagar la VM de origen' (--shutdown) para hacer una copia consistente")
    workdir = Path(opts.get("workdir", "/var/tmp/vm2morpheus")).resolve() / vm.name
    probe = workdir
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    # the downloaded disk files plus a converted copy; qcow2 only stores used blocks, so used size is a safe bound
    needed = (1 if source.direct else 2) * sum(d.used_bytes or d.size_bytes for d in vm.disks)
    if free < needed:
        problems.append(f"No hay espacio suficiente en {probe}: {human(free)} libres, hacen falta unos {human(needed)}; "
                        "elige otra carpeta de trabajo (p. ej. D:\\migracion) en un disco más grande")
    if dry_run:
        for p in problems:
            log(f"  fallaría: {p}")
        steps = " → ".join(STEP_LABELS[x] for x in relevant_steps(opts, mcfg))
        log(f"Simulación: se harían estos pasos: {steps}. No se ha modificado nada.")
        return {"dry_run": True, "problems": problems}
    if problems:
        raise RuntimeError("; ".join(problems))

    log(f"Carpeta de trabajo: {workdir}")
    workdir.mkdir(parents=True, exist_ok=True)
    st = State(workdir / "state.json")
    if st.done("upload") and not all(morpheus.image_exists(i["id"]) for i in st.d.get("images", [])):
        log("El progreso guardado dice que la imagen se subió, pero no está en este Morpheus: se empieza de cero")
        st.path.unlink(missing_ok=True)
        st = State(st.path)
    if opts.get("recreate_instance") and st.done("instance"):
        st.d["done"].remove("instance")  # redo only the last step; delete the old instance in Morpheus first
        st.d.pop("instance", None)
        st.path.write_text(json.dumps(st.d, indent=2))
    steps = relevant_steps(opts, mcfg, bool(st.d.get("injected")))
    done = [x for x in steps if st.done(x)]
    pending = [x for x in steps if not st.done(x)]
    if done and pending:
        log(f"Se continúa una migración anterior. Ya hecho: {labels(done)}. Falta: {labels(pending)}.")
        log(f"(Progreso guardado en {st.path})")
    elif done:
        names = ", ".join(f"{i['name']} (id {i['id']})" for i in st.d.get("images", []))
        log(f"Esta VM ya se migró por completo en una ejecución anterior (imágenes en Morpheus: {names}).")
        log(f"Para migrarla otra vez desde cero, borra {st.path}; para recrear solo la VM, marca "
            "'Recrear solo la instancia'.")

    bundle = bool(opts.get("multi_disk_bundle", True)) and len(vm.disks) > 1
    target = (opts.get("new_name") or "").strip() or vm.name  # name shown in Morpheus (image and VM)
    if target != vm.name:
        log(f"Nombre en Morpheus: {target!r} (en el origen es {vm.name!r})")
    gib = sum(d.used_bytes or d.size_bytes for d in vm.disks) / 2**30
    tracker = Tracker(steps, done, on_progress, weights=timings.predict_weights(steps, gib))

    check()
    if not st.done("fetch"):
        if vm.power_state == "on":
            log("Apagando la VM de origen ...")
            source.power_off(vm.name)
        log("Descargando discos ...")
        tracker.begin("fetch")
        n_disks = len(vm.disks)
        counter = itertools.count()  # a fallback to another download method asks for the bars again

        def fetch_bar(label, estimate=None):
            i = next(counter)
            tracker.update("fetch", min(1.0, i / n_disks))
            return Progress(label, cancel=cancel, estimate=estimate,
                            on_progress=lambda f, i=i: tracker.update("fetch", min(1.0, (i + f) / n_disks)))

        files = source.fetch_disks(vm, workdir, fetch_bar)
        st.mark("fetch", fetched=[str(f) for f in files])
        tracker.finish("fetch")

    check()
    if not st.done("convert"):
        log(f"Convirtiendo a {fmt} ...")
        tracker.begin("convert")
        out = []
        source.ensure_access([Path(p) for p in st.d["fetched"]])
        if windows_inject and not st.d.get("injected"):
            boot = Path(st.d["fetched"][0])
            need_vhdx = boot.suffix.lower() not in (".vhd", ".vhdx")  # Windows can only mount VHD/VHDX
            w_vhdx = 0.4 if need_vhdx else 0.0  # share of the "drivers" step: VHDX conversion, ISO, then DISM
            tracker.begin("drivers")
            if need_vhdx:
                log(f"Convirtiendo {boot.name} a VHDX para que Windows pueda montarlo ...")
                vhdx = boot.with_suffix(".vhdx")
                bar = Progress(f"convertir {boot.name}", percent=True, cancel=cancel,
                               on_progress=lambda f: tracker.update("drivers", w_vhdx * f, "convirtiendo el disco a VHDX", f))
                convert_disk(boot, vhdx, "vhdx", bar, cancel)
                bar.finish()
                if _inside(boot, workdir):
                    boot.unlink(missing_ok=True)
                st.set(fetched=[str(vhdx)] + st.d["fetched"][1:])  # saved now: the VMDK is gone, a retry must find the VHDX
            bar = Progress("ISO de virtio-win", cancel=cancel,
                           on_progress=lambda f: tracker.update("drivers", w_vhdx + 0.2 * f, "descargando virtio-win", f))
            iso = ensure_virtio_iso(opts.get("virtio_iso"), bar)
            tracker.update("drivers", w_vhdx + 0.2, "instalando los drivers con DISM", hide_percent=True)
            log("Inyectando drivers VirtIO en el disco de Windows (DISM) ...")
            added = inject_virtio_windows(Path(st.d["fetched"][0]), iso, log)
            log(f"Drivers añadidos: {', '.join(added)}")
            st.set(injected=True, fetched=st.d["fetched"])
            tracker.finish("drivers")
            tracker.begin("convert")
        for i, src in enumerate(map(Path, st.d["fetched"])):
            dst = workdir / (disk_file_name(vm.name, i) if bundle else f"{src.stem}.{fmt}")
            n_conv = len(st.d["fetched"])
            bar = Progress(f"convertir {src.name}", percent=True, cancel=cancel,
                           on_progress=lambda f, i=i: tracker.update("convert", (i + f) / n_conv))
            convert_disk(src, dst, fmt, bar, cancel)
            bar.finish()
            out.append(str(dst))
            if not opts.get("keep_files") and _inside(src, workdir):  # never delete the source VM's own disks
                src.unlink(missing_ok=True)
        st.mark("convert", converted=out)
        tracker.finish("convert")

    check()
    if not st.done("drivers"):
        if opts.get("inject_drivers") and not _is_windows():
            log("Inyectando drivers VirtIO (virt-v2v-in-place) ...")
            inject_drivers(Path(st.d["converted"][0]), vm.name, vm.memory_mb, vm.cpu,
                           vm.firmware == "uefi", fmt)
        st.mark("drivers")
    tracker.finish("drivers")

    check()
    if not st.done("upload"):
        tracker.begin("upload")

        def publish(path: Path, name: str, idx: int = 0, n: int = 1) -> dict:
            try:
                old_image = morpheus.find_one("virtual-images", "virtualImages", name)
            except MorpheusError:
                old_image = None  # only a courtesy warning: never fail the migration for it
            if old_image:
                log(f"Aviso: ya hay una imagen llamada {name!r} en Morpheus (id {old_image['id']}); se crea otra "
                    "con el mismo nombre. Borra la antigua de Library > Virtual Images si sobra.")
            log(f"Creando la imagen {name!r} en Morpheus ...")
            img = morpheus.create_virtual_image(
                name, mcfg.get("image_type", fmt), os_type=mcfg.get("os_type"),
                uefi=vm.firmware == "uefi", storage_provider_id=mcfg.get("storage_provider_id"),
                extra=mcfg.get("image_extra"))
            bar = Progress(f"subir {path.name}", cancel=cancel,
                           on_progress=lambda f: tracker.update("upload", (idx + f) / n))
            try:
                morpheus.upload_image(img["id"], path, bar)
            except CancelledError:
                try:
                    morpheus.delete_image(img["id"])  # do not leave a half-uploaded image behind
                    log(f"Se eliminó de Morpheus la imagen incompleta {name!r}")
                except Exception:
                    log(f"No se pudo eliminar la imagen incompleta {name!r} (id {img['id']}); bórrala a mano")
                raise
            bar.finish()
            morpheus.wait_image_ready(img["id"])
            return {"id": img["id"], "name": name}

        converted = [Path(p) for p in st.d["converted"]]
        images = []
        if bundle:
            log(f"Empaquetando {len(converted)} discos en una sola imagen multidisco (zip + metadata.json) ...")
            zip_path = workdir / f"{vm.name}.zip"
            build_bundle(vm.name, [(f, d.size_bytes) for f, d in zip(converted, vm.disks)], zip_path)
            images.append(publish(zip_path, target))
            if not opts.get("keep_files"):
                zip_path.unlink(missing_ok=True)
                for f in converted:
                    f.unlink(missing_ok=True)
        else:
            for i, f in enumerate(converted):
                images.append(publish(f, target if i == 0 else f"{target}-disk{i}", i, len(converted)))
                if not opts.get("keep_files"):
                    f.unlink(missing_ok=True)
        st.mark("upload", images=images, bundle=bundle)
        tracker.finish("upload")

    check()
    result = {"images": st.d["images"]}
    if not st.done("instance"):
        tracker.begin("instance")
        template = mcfg.get("instance")
        refcfg = mcfg.get("reference_instance")
        if refcfg or template:
            existing = morpheus.find_instance(target)
            if existing:  # checked before anything is built: a name clash is the commonest reason for a 400 here
                raise RuntimeError(instance_exists_message(target, existing))
        if refcfg:
            if "synthetic" in refcfg:
                ref = refcfg["synthetic"]
                if ref["layout"]["id"] is None:
                    layout_id, layout_name = morpheus.find_layout(ref["instanceType"]["code"])
                    ref["layout"]["id"] = layout_id
                    log(f"Layout detectado automáticamente: {layout_name} (id {layout_id})")
                if ref["config"].get("resourcePoolId") is None:
                    pools = morpheus.resource_pools(ref["cloud"]["id"])
                    ref["config"]["resourcePoolId"] = pools[0][0] if pools else "pool-1"
                    log("Resource pool " + (f"detectado automáticamente: {pools[0][1]} ({pools[0][0]})"
                                            if pools else "no detectable: se usa pool-1"))
            else:
                ref = morpheus.get_instance(refcfg["instance_id"])
            root_gb = -(-vm.disks[0].size_bytes // 2**30)
            in_bundle = st.d.get("bundle", False)  # one image carrying every disk (names match metadata.json)
            extra_gb = [-(-d.size_bytes // 2**30) for d in vm.disks[1:]]
            want_sata = bool(opts.get("boot_sata", True))

            def build(boot_sata: bool) -> dict:
                try:
                    return payload_from_reference(
                        ref, target, int(refcfg["image_id"]) if refcfg.get("image_id") else None,
                        st.d["images"][0]["id"], root_gb, extra_gb, uefi=vm.firmware == "uefi",
                        extra_name="data" if in_bundle else None, boot_sata=boot_sata)
                except MorpheusError:
                    dump = workdir / "reference-instance.json"
                    dump.write_text(json.dumps(redact(ref), indent=2, ensure_ascii=False), encoding="utf-8")
                    log(f"Instancia de referencia guardada (credenciales ocultas) en {dump}")
                    raise

            if extra_gb and not in_bundle:
                log(f"Nota: {len(extra_gb)} disco(s) extra se añaden como volúmenes EN BLANCO; sus datos se subieron "
                    "como imágenes aparte, pero no están conectados")
            elif extra_gb:
                log(f"Añadiendo {len(extra_gb)} volumen(es) de datos acorde con la imagen multidisco")
            if not st.d.get("injected"):
                log("Nota: no se instalaron drivers en la VM. " + (
                    "El disco de arranque se pide en un controlador SATA para que Windows arranque sin drivers VirtIO"
                    if want_sata else "un Windows necesita drivers VirtIO para arrancar desde VirtIO"))
            warning = plan_shortfall(morpheus.service_plan(ref["plan"]["id"]), vm.cpu, vm.memory_mb)
            if warning:
                log(warning)
            log("Creando la instancia en Morpheus ...")
            try:
                inst = create_instance_clearly(morpheus, build(want_sata), target)
            except MorpheusError as e:
                if not want_sata or not volume_related_rejection(e):
                    raise
                log(f"Morpheus rechazó la petición del controlador SATA ({e}); se reintenta sin SATA. "
                    "Cambia el disco de arranque a SATA a mano si Windows no arranca.")
                inst = create_instance_clearly(morpheus, build(False), target)
            st.mark("instance", instance={"id": inst.get("id"), "name": inst.get("name")})
        elif template:
            vals = {"image_id": st.d["images"][0]["id"], "image_name": st.d["images"][0]["name"],
                    "vm_name": target, "cpu": vm.cpu, "memory_mb": vm.memory_mb,
                    "memory_bytes": vm.memory_mb * 1024 * 1024}
            log("Creando la instancia en Morpheus ...")
            inst = create_instance_clearly(morpheus, render_template(template, vals), target)
            st.mark("instance", instance={"id": inst.get("id"), "name": inst.get("name")})
        else:
            log("No hay plantilla de instancia configurada: se termina tras subir la imagen")
            st.mark("instance")
        tracker.finish("instance")
    result["instance"] = st.d.get("instance")
    tracker.complete()
    timings.record(tracker.durations, gib)  # only steps run now: resumed ones have no duration
    log("Terminado.")
    return result


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False
