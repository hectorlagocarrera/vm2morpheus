#!/usr/bin/env bash
# Creates a local venv on first run, installs dependencies and opens the GUI.
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -e ".[all]"; }
exec .venv/bin/vm2morpheus gui
