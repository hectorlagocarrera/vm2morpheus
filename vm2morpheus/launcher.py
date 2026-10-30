"""Start-up wrapper for the windowed .exe: no console means no stdout/stderr, so log to a file and show errors."""
import ctypes
import sys
import traceback
from pathlib import Path

LOG = Path.home() / ".vm2morpheus" / "vm2morpheus.log"


def setup_streams():
    """Without a console Windows gives us sys.stdout/sys.stderr = None; point them at the log file."""
    if sys.stdout is not None and sys.stderr is not None:
        return None
    LOG.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOG, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = handle
    if sys.stderr is None:
        sys.stderr = handle
    return handle


def show_error(text: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, text, "vm2morpheus", 0x10)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def show_info(text: str) -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.MessageBoxW(0, text, "vm2morpheus", 0x40)
            return
        except Exception:
            pass
    print(text, file=sys.stderr)


def run(main, show_box: bool | None = None) -> int:
    """show_box: pop a message box on errors; by default only when there is no console to print to."""
    if show_box is None:
        show_box = sys.stdout is None or sys.stderr is None
    setup_streams()
    try:
        return main() or 0
    except SystemExit as e:
        if isinstance(e.code, str):  # sys.exit("message"): nobody would see it without a console
            print(e.code, file=sys.stderr)
            if show_box:
                show_error(e.code)
            return 1
        raise
    except Exception:
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        if show_box:
            show_error(f"Error inesperado:\n\n{detail[-1200:]}\n\nDetalle completo en {LOG}")
        return 1
