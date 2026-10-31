import pytest


@pytest.fixture(autouse=True)
def _isolated_user_files(tmp_path_factory, monkeypatch):
    """Learned timings and GUI settings live in ~/.vm2morpheus: keep tests away from the real one."""
    from vm2morpheus import settings, timings
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(timings, "PATH", home / "timings.json")
    monkeypatch.setattr(settings, "PATH", home / "settings.json")
