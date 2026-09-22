"""Portable launch plans and Windows account-directory permissions."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shlex
import sys

VERSION = "0.2.0"

BUNDLE_VARIABLES = ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "GI_TYPELIB_PATH",
                    "GIO_EXTRA_MODULES", "GSETTINGS_SCHEMA_DIR", "XDG_DATA_DIRS", "GSK_RENDERER")


def external_environment() -> dict[str, str]:
    """Keep bundled Python/GTK settings out of coding tools and terminals."""
    env = os.environ.copy()
    if env.pop("YOG_SOTHOTH_BUNDLED", None):
        for variable in BUNDLE_VARIABLES:
            prefix = "YOG_SOTHOTH_ORIGINAL_" + variable
            value = env.pop(prefix, "")
            if env.pop(prefix + "_SET", "") == "1":
                env[variable] = value
            else:
                env.pop(variable, None)
        env.pop("APPDIR", None)
        env.pop("APPIMAGE", None)
    return env


def check_history_links(root: Path) -> None:
    if os.name != "nt":
        return
    import tempfile
    from codex_switcher import SwitcherError
    try:
        with tempfile.TemporaryDirectory(prefix=".link-check-", dir=root) as directory:
            probe = Path(directory)
            (probe / "target").mkdir()
            (probe / "directory-link").symlink_to(probe / "target", target_is_directory=True)
            (probe / "target-file").touch()
            (probe / "file-link").symlink_to(probe / "target-file")
    except OSError:
        raise SwitcherError("Shared history requires Windows Developer Mode or permission to create symbolic links. "
                            "Enable it before migrating; per-account switching works without it.") from None


def cli_command(arguments: list[str], tool: str | None = None) -> list[str]:
    # A terminal needs its own AppImage mount after the GUI has been closed.
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.environ.get("APPDIR"):
        return [appimage, *([tool] if tool else []), *arguments]
    module = f"{tool}_switcher.py" if tool else "switcher_cli.py"
    return [sys.executable, str(Path(__file__).with_name(module)), *arguments]


def shell_function(tool: str, executable: str, shell: str) -> str:
    command = cli_command(["run", "--"], tool)
    variable = f"{tool.upper()}_SWITCHER_{tool.upper()}"
    if shell == "powershell":
        def quote(value):
            return "'" + value.replace("'", "''") + "'"
        invoke = " ".join(quote(arg) for arg in command)
        return (f"function global:{tool} {{\n"
                f"  $previousExecutable = [Environment]::GetEnvironmentVariable('{variable}', 'Process')\n"
                f"  try {{\n"
                f"    $env:{variable} = {quote(executable)}\n"
                f"    & {invoke} @args\n"
                f"  }} finally {{\n"
                f"    [Environment]::SetEnvironmentVariable('{variable}', $previousExecutable, 'Process')\n"
                f"  }}\n"
                f"}}")
    invoke = shlex.join(command)
    return f'{tool}() {{\n  {variable}={shlex.quote(executable)} {invoke} "$@"\n}}'


def private_windows_directory(path: Path) -> None:
    """Replace inherited access with the current user, SYSTEM, and administrators."""
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    token, sid, descriptor = wintypes.HANDLE(), wintypes.LPWSTR(), ctypes.c_void_p()
    try:
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, user, size.value, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        user_sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not advapi.ConvertSidToStringSidW(user_sid, ctypes.byref(sid)):
            raise ctypes.WinError(ctypes.get_last_error())
        sddl = f"D:P(A;OICI;FA;;;{sid.value})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if descriptor:
            kernel.LocalFree(descriptor)
        if sid:
            kernel.LocalFree(ctypes.cast(sid, ctypes.c_void_p))
        if token:
            kernel.CloseHandle(token)
