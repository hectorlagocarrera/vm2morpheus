"""Embedded window for the GUI (pywebview on top of Edge WebView2) instead of the user's browser."""
import sys

_WEBVIEW2 = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_installed() -> bool:
    """The Evergreen WebView2 runtime. Without it pywebview falls back to the old IE engine, which cannot run
    this page, so we prefer the normal browser in that case."""
    if sys.platform != "win32":
        return False
    import winreg
    sep = "\\"
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE" + sep + "WOW6432Node" + sep + _WEBVIEW2),
                      (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE" + sep + _WEBVIEW2),
                      (winreg.HKEY_CURRENT_USER, "Software" + sep + _WEBVIEW2)):
        try:
            with winreg.OpenKey(hive, key) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
            if version and version != "0.0.0.0":
                return True
        except OSError:
            continue
    return False


def open_window(url: str, busy, title: str = "vm2morpheus") -> bool:
    """Show the GUI in a native window and block until it is closed. Returns False if no window could be shown
    (library or runtime missing), so the caller can fall back to the browser."""
    if sys.platform == "win32" and not webview2_installed():
        return False
    try:
        import webview
    except Exception:
        return False
    try:
        window = webview.create_window(title, url, width=1150, height=920, min_size=(900, 600),
                                       text_select=True)  # text_select: the log must be copyable

        def on_closing():
            if busy():
                return window.create_confirmation_dialog(title, "Hay una migración en curso. ¿Cerrar igualmente?")
            return True

        window.events.closing += on_closing
        webview.start(gui="edgechromium" if sys.platform == "win32" else None)
        return True
    except Exception:
        return False
