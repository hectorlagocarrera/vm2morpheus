# vm2morpheus

One command to move a VM from Hyper-V or VMware to Morpheus (VM Essentials / KVM):

    export SOURCE_PASSWORD=... MORPHEUS_TOKEN=...
    vm2morpheus migrate -c config.yaml --vm web01 --shutdown

Pipeline: inspect -> (power off) -> download disks -> `qemu-img convert` -> (virt-v2v-in-place) ->
`POST /api/virtual-images` + `/upload` -> optional `POST /api/instances` from your template.
Progress is checkpointed in `<workdir>/<vm>/state.json`; re-running resumes.

## GUI

    Windows: double-click run_gui.bat (installs Python + QEMU via winget if missing)
    Linux/macOS: ./run_gui.sh (needs python3 and qemu-utils already installed)

Opens http://127.0.0.1:8765 (local only). Fill in origin, Morpheus URL + API token, press
"Simulación" first, then "Migrar". Needs Python 3.10+ and `qemu-img` on PATH.

## Creating the VM in Morpheus from the uploaded image

Morpheus does not boot a VM straight from a Virtual Image: it needs a Node Type and a Layout that point at it.
Once, per image (UI names vary a little between versions):
1. Library > Blueprints > Node Types > Add: provision type = your KVM/VME type, VM Image = the uploaded image.
2. Library > Blueprints > Layouts > Add: pick an instance type, attach that Node Type, save. Note its ID.
3. Provision > Instances > Add: choose that instance type + layout, then group/cloud/plan/network.
Put the layout ID in the GUI (step 3) or in `morpheus.instance`, and the tool does step 3 for you.

Other commands: `inspect` (show VM as seen by the source), `check` (list Morpheus clouds, verifies the token),
`--dry-run` (preflight only).

## Status — read before using

Tested: pipeline, convert (real qemu-img), upload and instance creation against a **fake** Morpheus server.
**Not tested** against real Hyper-V, vCenter/ESXi or a real Morpheus appliance. Known open points:
- valid `imageType` for KVM and the image `status` values come from public docs only;
- an instance needs a layout/node type pointing at the image: supply the payload via `morpheus.instance`;
- only the boot disk is attached by the instance template; extra disks are uploaded as separate images;
- `--inject-drivers` (virt-v2v-in-place) is experimental; Windows needs virtio-win;
- no live/incremental P2V: physical machines must be captured first (Disk2vhd / dd) and passed as a disk.

Requires `qemu-img` (qemu-utils). Optional extras: `pip install -e '.[all]'`.


## Antivirus

The Windows build is an unsigned PyInstaller program that mounts disks, runs PowerShell/DISM and needs
administrator rights, so some antivirus products flag it heuristically. To check it is the file GitHub built,
compare its SHA-256 with `SHA256.txt` (shipped in the zip) and with the "Bundle qemu-img ... SHA-256" step of the
workflow log. A code-signing certificate, or an allow-list entry in the corporate antivirus by hash/path, is the
real fix; submitting the file as a false positive to the vendor helps too.


## Console and browser

The packaged program keeps its console window (it shows the log and closing it stops the app) and opens the
interface in your default browser. An embedded window (`vm2morpheus gui --ventana`, needs `pip install pywebview`)
and a hidden console are not used in the .exe: both made antivirus engines more suspicious.


## Icons

The icon (exe, Explorer, taskbar) and the browser favicon come from the Redes globe in `assets/globe-source.png`,
turned into a transparent globe on a white rounded tile. Regenerate them with `python tools/make_icons.py`
(needs Pillow); it rewrites `assets/*.ico`, `assets/icon-192.png` and `vm2morpheus/icon_data.py`.


## History and clean-up

Every migration started from the GUI is written to `~/.vm2morpheus/history.json` (date, VM, source, steps and their
durations, average download/upload speed, a throughput curve, the error if it failed). The "Histórico" tab lists them,
shows a detail view with a chart, and exports CSV. When a migration has created the VM, a dialog asks you to test it
and only then, on explicit confirmation, deletes the library image it uploaded (and optionally the work folder).


## Hot copy (Hyper-V)

With "Copia en caliente" a running Hyper-V VM is not powered off: a temporary checkpoint (`vm2morpheus-<date>`) freezes
the original VHDX files, which are copied as plain files while the VM keeps running; the checkpoint is removed as soon
as the copy ends (also after an error or a cancel). The copy is a point in time: what the VM writes after the
checkpoint is not in it, and the source keeps running, so power it off before starting the migrated VM. Requires that
the VM has no checkpoints of its own and that checkpoints are enabled on it (`Set-VM -CheckpointType Production`).
