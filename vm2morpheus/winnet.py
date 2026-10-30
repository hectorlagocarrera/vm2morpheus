"""Windows SMB sessions through the Win32 API, so the password never appears on a command line."""
import ctypes

ERRORS = {
    53: "no se encuentra la ruta de red (revisa la IP y que el puerto 445 esté abierto)",
    86: "contraseña incorrecta",
    1219: "ya hay una conexión con ese servidor con otras credenciales (ciérrala o reinicia la sesión)",
    1326: "usuario o contraseña incorrectos",
    1244: "acceso no autorizado",
}


def add_connection(share: str, user: str, password: str) -> None:
    """Authenticate to \\\\host\\X$ (WNetAddConnection2W). Raises RuntimeError with a readable reason."""
    from ctypes import wintypes

    class NETRESOURCEW(ctypes.Structure):
        _fields_ = [("dwScope", wintypes.DWORD), ("dwType", wintypes.DWORD), ("dwDisplayType", wintypes.DWORD),
                    ("dwUsage", wintypes.DWORD), ("lpLocalName", wintypes.LPWSTR),
                    ("lpRemoteName", wintypes.LPWSTR), ("lpComment", wintypes.LPWSTR),
                    ("lpProvider", wintypes.LPWSTR)]

    resource = NETRESOURCEW()
    resource.dwType = 1  # RESOURCETYPE_DISK
    resource.lpRemoteName = share
    code = ctypes.WinDLL("mpr").WNetAddConnection2W(ctypes.byref(resource), password, user, 0)
    if code not in (0, 1219):  # 1219 is handled below: the share may already be open with the same account
        raise RuntimeError(f"No se puede abrir {share}: {ERRORS.get(code, f'error de Windows {code}')}")
    if code == 1219:
        raise RuntimeError(f"No se puede abrir {share}: {ERRORS[1219]}")


def cancel_connection(share: str) -> None:
    try:
        ctypes.WinDLL("mpr").WNetCancelConnection2W(share, 0, True)
    except (OSError, AttributeError):
        pass
