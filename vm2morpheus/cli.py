import argparse
import json
import sys

import yaml

from . import __version__

from .morpheus import MorpheusClient
from .pipeline import migrate
from .sources import make_source
from .util import resolve_secret


def build_parser():
    p = argparse.ArgumentParser(prog="vm2morpheus", description="Migra VMs de Hyper-V/VMware a Morpheus")
    p.add_argument("command", nargs="?", default="gui", choices=["inspect", "check", "migrate", "gui"])
    p.add_argument("-c", "--config", help="fichero de configuración YAML")
    p.add_argument("--vm", help="nombre de la VM en el origen")
    p.add_argument("--source-type", choices=["hyperv", "vmware"])
    p.add_argument("--source-host")
    p.add_argument("--source-user")
    p.add_argument("--morpheus-url")
    p.add_argument("--os", choices=["windows", "linux", "other"], help="sistema operativo invitado (Hyper-V no lo informa)")
    p.add_argument("--shutdown", action="store_true", help="apagar la VM de origen si está encendida")
    p.add_argument("--inject-drivers", action="store_true", help="inyectar drivers VirtIO (Windows: DISM; Linux: virt-v2v-in-place)")
    p.add_argument("--dry-run", action="store_true")
    return p


def load_config(args) -> dict:
    cfg = yaml.safe_load(open(args.config)) if args.config else {}
    cfg.setdefault("source", {})
    cfg.setdefault("morpheus", {})
    cfg.setdefault("options", {})
    if args.source_type:
        cfg["source"]["type"] = args.source_type
    if args.source_host:
        cfg["source"]["host"] = args.source_host
    if args.source_user:
        cfg["source"]["username"] = args.source_user
    if args.morpheus_url:
        cfg["morpheus"]["url"] = args.morpheus_url
    if args.os:
        cfg["source"]["os_family"] = args.os
    if args.shutdown:
        cfg["options"]["shutdown_source"] = True
    if args.inject_drivers:
        cfg["options"]["inject_drivers"] = True
    cfg["source"].setdefault("password", "env:SOURCE_PASSWORD")
    cfg["morpheus"].setdefault("token", "env:MORPHEUS_TOKEN")
    return cfg


def main(argv=None):
    args = build_parser().parse_args(argv)
    print(f"vm2morpheus {__version__}", file=sys.stderr)
    if args.command == "gui":
        from .gui import serve
        serve()
        return 0
    cfg = load_config(args)
    m = cfg["morpheus"]
    if not m.get("url"):
        sys.exit("Falta la URL de Morpheus (--morpheus-url o el fichero de configuración)")
    morpheus = MorpheusClient(m["url"], resolve_secret(m["token"]), verify=m.get("verify_ssl", True))

    if args.command == "check":
        for z in morpheus.clouds():
            print(f"nube {z['id']}: {z['name']} ({z.get('zoneType', {}).get('name', '?')})")
        return 0

    source = make_source(cfg["source"])
    source.connect()
    try:
        if not args.vm:
            sys.exit("Falta --vm (nombre de la VM)")
        if args.command == "inspect":
            from dataclasses import asdict
            print(json.dumps(asdict(source.get_vm(args.vm)), indent=2))
            return 0
        migrate(source, morpheus, args.vm, cfg["options"], m, dry_run=args.dry_run)
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
