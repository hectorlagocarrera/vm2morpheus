import json
import shutil
from pathlib import Path

from .convert import convert_disk, inject_drivers
from .morpheus import MorpheusClient, render_template
from .sources.base import Source
from .util import Progress, human

STEPS = ["fetch", "convert", "drivers", "upload", "instance"]


class State:
    """Tiny JSON checkpoint so a failed run resumes instead of re-downloading hundreds of GB."""

    def __init__(self, path: Path):
        self.path = path
        self.d = json.loads(path.read_text()) if path.exists() else {}

    def done(self, step):
        return step in self.d.get("done", [])

    def mark(self, step, **data):
        self.d.setdefault("done", []).append(step)
        self.d.update(data)
        self.path.write_text(json.dumps(self.d, indent=2))


def migrate(source: Source, morpheus: MorpheusClient, vm_name: str, opts: dict, mcfg: dict,
            dry_run=False, log=print) -> dict:
    vm = source.get_vm(vm_name)
    fmt = opts.get("output_format", "qcow2")
    total = sum(d.size_bytes for d in vm.disks)
    log(f"VM {vm.name}: {vm.cpu} vCPU, {vm.memory_mb} MiB, {vm.firmware.upper()}, "
        f"{vm.os_family}, {vm.power_state}, {len(vm.disks)} disk(s), {human(total)} provisioned")

    problems = []
    if vm.has_snapshots:
        problems.append("VM has snapshots/checkpoints; consolidate them first")
    if not vm.disks:
        problems.append("VM has no disks")
    if vm.power_state == "on" and not opts.get("shutdown_source"):
        problems.append("VM is running; pass --shutdown to power it off (consistent copy)")
    workdir = Path(opts.get("workdir", "/var/tmp/vm2morpheus")) / vm.name
    free = shutil.disk_usage(workdir.parent if workdir.parent.exists() else Path("/")).free
    if free < total * 2:
        log(f"warning: {human(free)} free in workdir, up to {human(total * 2)} may be needed")
    if dry_run:
        for p in problems:
            log(f"  would fail: {p}")
        log("dry-run: steps = " + " -> ".join(STEPS) + f" (target format {fmt})")
        return {"dry_run": True, "problems": problems}
    if problems:
        raise RuntimeError("; ".join(problems))

    workdir.mkdir(parents=True, exist_ok=True)
    st = State(workdir / "state.json")

    if not st.done("fetch"):
        if vm.power_state == "on":
            log("powering off source VM ...")
            source.power_off(vm.name)
        log("fetching disks ...")
        files = source.fetch_disks(vm, workdir, lambda label: _bar(label))
        st.mark("fetch", fetched=[str(f) for f in files])

    if not st.done("convert"):
        log(f"converting to {fmt} ...")
        out = []
        for src in map(Path, st.d["fetched"]):
            dst = src.with_suffix(f".{fmt}")
            bar = Progress(f"convert {src.name}")
            convert_disk(src, dst, fmt, bar)
            bar.finish()
            out.append(str(dst))
            if not opts.get("keep_files"):
                src.unlink(missing_ok=True)
        st.mark("convert", converted=out)

    if not st.done("drivers"):
        if opts.get("inject_drivers"):
            log("injecting virtio drivers (virt-v2v-in-place) ...")
            inject_drivers(Path(st.d["converted"][0]), vm.name, vm.memory_mb, vm.cpu,
                           vm.firmware == "uefi", fmt)
        st.mark("drivers")

    if not st.done("upload"):
        images = []
        for i, f in enumerate(map(Path, st.d["converted"])):
            name = f"{vm.name}" if i == 0 else f"{vm.name}-disk{i}"
            log(f"creating Morpheus image {name!r} ...")
            img = morpheus.create_virtual_image(
                name, mcfg.get("image_type", fmt), os_type=mcfg.get("os_type"),
                uefi=vm.firmware == "uefi", storage_provider_id=mcfg.get("storage_provider_id"),
                extra=mcfg.get("image_extra"))
            bar = Progress(f"upload {f.name}")
            morpheus.upload_image(img["id"], f, bar)
            bar.finish()
            morpheus.wait_image_ready(img["id"])
            images.append({"id": img["id"], "name": name})
            if not opts.get("keep_files"):
                f.unlink(missing_ok=True)
        st.mark("upload", images=images)

    result = {"images": st.d["images"]}
    if not st.done("instance"):
        template = mcfg.get("instance")
        if template:
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


def _bar(label):
    bar = Progress(label)
    return bar
