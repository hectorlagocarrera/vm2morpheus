import argparse
import json
import sys

import yaml

from .morpheus import MorpheusClient
from .pipeline import migrate
from .sources import make_source
from .util import resolve_secret


def build_parser():
    p = argparse.ArgumentParser(prog="vm2morpheus", description="Migrate VMs from Hyper-V/VMware to Morpheus")
    p.add_argument("command", nargs="?", default="gui", choices=["inspect", "check", "migrate", "gui"])
    p.add_argument("-c", "--config", help="YAML config file")
    p.add_argument("--vm", help="name of the VM on the source")
    p.add_argument("--source-type", choices=["hyperv", "vmware"])
    p.add_argument("--source-host")
    p.add_argument("--source-user")
    p.add_argument("--morpheus-url")
    p.add_argument("--os", choices=["windows", "linux", "other"], help="guest OS (Hyper-V cannot tell us)")
    p.add_argument("--shutdown", action="store_true", help="power off the source VM if it is running")
    p.add_argument("--inject-drivers", action="store_true", help="run virt-v2v-in-place (experimental)")
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
    if args.command == "gui":
        from .gui import serve
        serve()
        return 0
    cfg = load_config(args)
    m = cfg["morpheus"]
    if not m.get("url"):
        sys.exit("morpheus url missing (--morpheus-url or config)")
    morpheus = MorpheusClient(m["url"], resolve_secret(m["token"]), verify=m.get("verify_ssl", True))

    if args.command == "check":
        for z in morpheus.clouds():
            print(f"cloud {z['id']}: {z['name']} ({z.get('zoneType', {}).get('name', '?')})")
        return 0

    source = make_source(cfg["source"])
    source.connect()
    try:
        if not args.vm:
            sys.exit("--vm is required")
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
