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

def _is_windows() -> bool:
    return os.name == "nt"


STEPS = ["fetch", "convert", "drivers", "upload", "instance"]


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
            dry_run=False, log=print, cancel=None) -> dict:
    def check():
        if cancel is not None and cancel.is_set():
            raise CancelledError("cancelled")

    vm = source.get_vm(vm_name)
    fmt = opts.get("output_format", "qcow2")
    windows_inject = bool(opts.get("inject_drivers")) and _is_windows() and vm.os_family == "windows"
    if windows_inject and source.direct:
        source.direct = False  # drivers are written into the disk, so it must be a private copy, never the original
        log("driver injection needs a local copy of the disk: direct read is turned off")
    total = sum(d.size_bytes for d in vm.disks)
    log(f"VM {vm.name}: {vm.cpu} vCPU, {vm.memory_mb} MiB, {vm.firmware.upper()}, "
        f"{vm.os_family}, {vm.power_state}, {len(vm.disks)} disk(s), {human(total)} provisioned")

    problems = []
    if vm.has_snapshots:
        problems.append("VM has snapshots/checkpoints; consolidate them first")
    if not vm.disks:
        problems.append("VM has no disks")
    if windows_inject and not is_admin():
        problems.append("driver injection needs administrator rights: close the program and start it with "
                        "right click > Run as administrator (or untick the drivers option)")
    if vm.power_state == "on" and not opts.get("shutdown_source"):
        problems.append("VM is running; pass --shutdown to power it off (consistent copy)")
    workdir = Path(opts.get("workdir", "/var/tmp/vm2morpheus")).resolve() / vm.name
    probe = workdir
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    # the downloaded disk files plus a converted copy; qcow2 only stores used blocks, so used size is a safe bound
    needed = (1 if source.direct else 2) * sum(d.used_bytes or d.size_bytes for d in vm.disks)
    if free < needed:
        problems.append(f"not enough free space in {probe}: {human(free)} free, about {human(needed)} needed; "
                        "set another work folder (e.g. D:\\migracion) on a bigger disk")
    if dry_run:
        for p in problems:
            log(f"  would fail: {p}")
        log("dry-run: steps = " + " -> ".join(STEPS) + f" (target format {fmt})")
        return {"dry_run": True, "problems": problems}
    if problems:
        raise RuntimeError("; ".join(problems))

    log(f"work folder: {workdir}")
    workdir.mkdir(parents=True, exist_ok=True)
    st = State(workdir / "state.json")
    if st.done("upload") and not all(morpheus.image_exists(i["id"]) for i in st.d.get("images", [])):
        log("the saved progress says the image was uploaded, but it is not in this Morpheus: starting from scratch")
        st.path.unlink(missing_ok=True)
        st = State(st.path)
    if opts.get("recreate_instance") and st.done("instance"):
        st.d["done"].remove("instance")  # redo only the last step; delete the old instance in Morpheus first
        st.d.pop("instance", None)
        st.path.write_text(json.dumps(st.d, indent=2))
    skipped = [x for x in STEPS if st.done(x)]
    if skipped:
        log(f"resuming from {st.path}: already done: {', '.join(skipped)}")
        if len(skipped) == len(STEPS):
            log("everything was already completed in a previous run. Images in Morpheus: "
                + ", ".join(f"{i['name']} (id {i['id']})" for i in st.d.get("images", [])))
            log(f"to migrate this VM again from scratch, delete {st.path}")

    bundle = bool(opts.get("multi_disk_bundle", True)) and len(vm.disks) > 1

    check()
    if not st.done("fetch"):
        if vm.power_state == "on":
            log("powering off source VM ...")
            source.power_off(vm.name)
        log("fetching disks ...")
        files = source.fetch_disks(vm, workdir, lambda label: Progress(label, cancel=cancel))
        st.mark("fetch", fetched=[str(f) for f in files])

    check()
    if not st.done("convert"):
        log(f"converting to {fmt} ...")
        out = []
        source.ensure_access([Path(p) for p in st.d["fetched"]])
        if windows_inject and not st.d.get("injected"):
            boot = Path(st.d["fetched"][0])
            if boot.suffix.lower() not in (".vhd", ".vhdx"):  # Windows can only mount VHD/VHDX
                log(f"converting {boot.name} to VHDX so Windows can mount it ...")
                vhdx = boot.with_suffix(".vhdx")
                bar = Progress(f"convert {boot.name}", percent=True, cancel=cancel)
                convert_disk(boot, vhdx, "vhdx", bar, cancel)
                bar.finish()
                if _inside(boot, workdir):
                    boot.unlink(missing_ok=True)
                st.d["fetched"][0] = str(vhdx)
            bar = Progress("virtio-win ISO", cancel=cancel)
            iso = ensure_virtio_iso(opts.get("virtio_iso"), bar)
            log("injecting VirtIO drivers into the Windows disk (DISM) ...")
            added = inject_virtio_windows(Path(st.d["fetched"][0]), iso, log)
            log(f"drivers added: {', '.join(added)}")
            st.set(injected=True, fetched=st.d["fetched"])
        for i, src in enumerate(map(Path, st.d["fetched"])):
            dst = workdir / (disk_file_name(vm.name, i) if bundle else f"{src.stem}.{fmt}")
            bar = Progress(f"convert {src.name}", percent=True, cancel=cancel)
            convert_disk(src, dst, fmt, bar, cancel)
            bar.finish()
            out.append(str(dst))
            if not opts.get("keep_files") and _inside(src, workdir):  # never delete the source VM's own disks
                src.unlink(missing_ok=True)
        st.mark("convert", converted=out)

    check()
    if not st.done("drivers"):
        if opts.get("inject_drivers") and not _is_windows():
            log("injecting virtio drivers (virt-v2v-in-place) ...")
            inject_drivers(Path(st.d["converted"][0]), vm.name, vm.memory_mb, vm.cpu,
                           vm.firmware == "uefi", fmt)
        st.mark("drivers")

    check()
    if not st.done("upload"):
        def publish(path: Path, name: str) -> dict:
            log(f"creating Morpheus image {name!r} ...")
            img = morpheus.create_virtual_image(
                name, mcfg.get("image_type", fmt), os_type=mcfg.get("os_type"),
                uefi=vm.firmware == "uefi", storage_provider_id=mcfg.get("storage_provider_id"),
                extra=mcfg.get("image_extra"))
            bar = Progress(f"upload {path.name}", cancel=cancel)
            try:
                morpheus.upload_image(img["id"], path, bar)
            except CancelledError:
                try:
                    morpheus.delete_image(img["id"])  # do not leave a half-uploaded image behind
                    log(f"removed the incomplete image {name!r} from Morpheus")
                except Exception:
                    log(f"could not remove the incomplete image {name!r} (id {img['id']}); delete it by hand")
                raise
            bar.finish()
            morpheus.wait_image_ready(img["id"])
            return {"id": img["id"], "name": name}

        converted = [Path(p) for p in st.d["converted"]]
        images = []
        if bundle:
            log(f"packing {len(converted)} disks into one multi-disk image (zip + metadata.json) ...")
            zip_path = workdir / f"{vm.name}.zip"
            build_bundle(vm.name, [(f, d.size_bytes) for f, d in zip(converted, vm.disks)], zip_path)
            images.append(publish(zip_path, vm.name))
            if not opts.get("keep_files"):
                zip_path.unlink(missing_ok=True)
                for f in converted:
                    f.unlink(missing_ok=True)
        else:
            for i, f in enumerate(converted):
                images.append(publish(f, vm.name if i == 0 else f"{vm.name}-disk{i}"))
                if not opts.get("keep_files"):
                    f.unlink(missing_ok=True)
        st.mark("upload", images=images, bundle=bundle)

    check()
    result = {"images": st.d["images"]}
    if not st.done("instance"):
        template = mcfg.get("instance")
        refcfg = mcfg.get("reference_instance")
        if refcfg:
            ref = refcfg["synthetic"] if "synthetic" in refcfg else morpheus.get_instance(refcfg["instance_id"])
            root_gb = -(-vm.disks[0].size_bytes // 2**30)
            in_bundle = st.d.get("bundle", False)  # one image carrying every disk (names match metadata.json)
            extra_gb = [-(-d.size_bytes // 2**30) for d in vm.disks[1:]]
            want_sata = bool(opts.get("boot_sata", True))

            def build(boot_sata: bool) -> dict:
                try:
                    return payload_from_reference(
                        ref, vm.name, int(refcfg["image_id"]) if refcfg.get("image_id") else None,
                        st.d["images"][0]["id"], root_gb, extra_gb, uefi=vm.firmware == "uefi",
                        extra_name="data" if in_bundle else None, boot_sata=boot_sata)
                except MorpheusError:
                    dump = workdir / "reference-instance.json"
                    dump.write_text(json.dumps(redact(ref), indent=2, ensure_ascii=False), encoding="utf-8")
                    log(f"saved the reference instance (credentials hidden) to {dump}")
                    raise

            if extra_gb and not in_bundle:
                log(f"note: {len(extra_gb)} extra disk(s) are added as BLANK volumes; their data was uploaded "
                    "as separate images but is not attached")
            elif extra_gb:
                log(f"adding {len(extra_gb)} data volume(s) matching the multi-disk image")
            if not st.d.get("injected"):
                log("note: no guest drivers were installed. " + (
                    "The boot disk is requested on a SATA controller so Windows can boot without VirtIO drivers"
                    if want_sata else "a Windows guest needs VirtIO drivers to boot from VirtIO"))
            log("creating Morpheus instance (cloned from the reference instance) ...")
            try:
                inst = morpheus.create_instance(build(want_sata))
            except MorpheusError as e:
                if not want_sata or " 400:" not in str(e):
                    raise
                log(f"Morpheus rejected the SATA controller request ({e}); retrying without it. "
                    "Change the boot disk to SATA by hand if Windows does not boot.")
                inst = morpheus.create_instance(build(False))
            st.mark("instance", instance={"id": inst.get("id"), "name": inst.get("name")})
        elif template:
            vals = {"image_id": st.d["images"][0]["id"], "image_name": st.d["images"][0]["name"],
                    "vm_name": vm.name, "cpu": vm.cpu, "memory_mb": vm.memory_mb,
                    "memory_bytes": vm.memory_mb * 1024 * 1024}
            log("creating Morpheus instance ...")
            inst = morpheus.create_instance(render_template(template, vals))
            st.mark("instance", instance={"id": inst.get("id"), "name": inst.get("name")})
        else:
            log("no 'morpheus.instance' template configured: stopping after image upload")
            st.mark("instance")
    result["instance"] = st.d.get("instance")
    log("done.")
    return result


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False
