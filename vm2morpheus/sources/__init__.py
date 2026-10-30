from ..util import resolve_secret
from .base import Source


def make_source(cfg: dict) -> Source:
    kind = cfg.get("type")
    cfg = {**cfg, "password": resolve_secret(cfg.get("password"))}
    if kind == "hyperv":
        from .hyperv import HyperVSource
        return HyperVSource(cfg)
    if kind == "vmware":
        from .vmware import VMwareSource
        return VMwareSource(cfg)
    raise SystemExit(f"unknown source type {kind!r} (expected 'hyperv' or 'vmware')")
