"""Remembers the last values typed in the GUI. Secrets are stored only if the user opts in."""
import json
import os
from pathlib import Path

SECRET_KEYS = ("source_password", "morpheus_token")
PATH = Path.home() / ".vm2morpheus" / "settings.json"


def load() -> dict:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(form: dict) -> None:
    data = dict(form)
    if "auto_workdir" not in data and load().get("auto_workdir"):
        data["auto_workdir"] = load()["auto_workdir"]  # chosen once so resumes find the same folder
    if not data.get("remember_secrets"):
        for k in SECRET_KEYS:
            data.pop(k, None)
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(PATH, 0o600)  # no-op on Windows
    except OSError:
        pass
